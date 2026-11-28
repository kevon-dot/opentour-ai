"""OPE-528 x OPE-571: an ``independent_verdict`` verdict consumed by ``comparison_packet``.

The evaluator is handed only the OPE-571 slice-substituted operating point (every slice name
already a keyed digest) and never the slice key or a plain name. Its verdict must declare
``operating_point_digest_form: "slice_substituted.v1"`` over exactly the digest the packet
recomputes from the comparison under the engagement key, so the packet no longer refuses it with
``operating_point_digest_predates_slice_digests``. Every other hold still applies.

Generated sentinel fixtures only: comparisons are real ``comparison_plan`` runs over generated
rows, the evaluator population is generated, and the slice names are the generated
``SLICE_NAMES`` sentinels. These tests prove software behavior only and establish no provider,
customer or separated-custody evidence.
"""

from __future__ import annotations

import copy
import hashlib

import pytest
from opentour_cell.pilot import comparison_packet as cp
from opentour_cell.pilot import independent_verdict as iv
from opentour_cell.pilot.operating_point import digest
from test_comparison_packet import (
    KEYS,
    SLICE_NAMES,
    STRICT,
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
    CLEAN_WIN,
    WORKER,
    Replay,
    Scenario,
    evaluator,
    receipt_for,
)
from test_independent_verdict import claim as iv_claim
from test_independent_verdict import policy as iv_policy

HOLD_TERMS = {**STRICT, "minimum_verified_cases": 50}


def named_run(root, *, critical=SLICE_NAMES, hold=False, material=()):
    """A real comparison whose terms name generated sentinel slices."""
    root.mkdir(mode=0o700, parents=True)
    dev, protected = rows("dev"), rows("protected")
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
    return comparison


def substituted(comparison, key=None):
    """What the customer worker hands the evaluator: names replaced by their keyed digests."""
    return cp.slice_substituted_terms(
        comparison["terms"], key or key_for(comparison), **scope(comparison)
    )


class Issued:
    """One verdict issued by the real evaluator for a claim bound to ``comparison``."""

    def __init__(self, comparison, root, *, terms=None, point=None, critical=None):
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

        def slice_of(index, codes):
            return critical if index < 20 else ()

        s = Scenario(CLEAN_WIN, comparators=("incumbent",), slice_of=slice_of)
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


def assemble(comparison, issued, verdict=None, **kw):
    return cp.assemble_packet(
        comparison,
        issued.receipt,
        issued.doc if verdict is None else verdict,
        issued.ctx,
        slice_key=key_for(comparison),
        **kw,
    )


def stage(comparison, issued, root, verdict=None):
    """Stage through a LIVE outbox (no fixture flag), as production constructs it."""
    root.mkdir(mode=0o700, parents=True)
    store = cp.PacketOutbox(root / "live.sqlite3", slice_keys=KEYS)
    assert store.allow_generated_fixture is False

    class _Authority:
        def current(self, run_ref):
            return issued.ctx if run_ref == issued.ctx.run_ref else None

    out = store.stage(
        comparison,
        issued.receipt,
        issued.doc if verdict is None else verdict,
        authority=_Authority(),
        run_ref=issued.ctx.run_ref,
        now=1_000,
    )
    return out, store


# ------------------------------------------------------------------- positive: the 528 form


def test_528_verdict_declares_the_slice_substituted_form_over_the_packets_own_digest(tmp_path):
    comparison = named_run(tmp_path / "run", hold=True)
    issued = Issued(comparison, tmp_path / "eval")
    doc = issued.doc
    assert doc["outcome"] == "verified_gain" and doc["verdict"] == "reproduced", doc["reasons"]
    assert doc["operating_point_digest_form"] == cp.OPERATING_POINT_DIGEST_FORM
    # Exactly the digest the packet derives from the comparison under the engagement key.
    assert doc["operating_point_digest"] == cp.operating_point_digest(substituted(comparison))
    assert cp._verdict_ok(doc)
    scoped = cp._scoped_key(
        key_for(comparison), comparison["organization"], comparison["engagement"]
    )
    assert cp._point_binding(doc, comparison, scoped) is None


def test_packet_accepts_a_528_verdict_on_the_hold_path(tmp_path):
    comparison = named_run(tmp_path / "run", hold=True)
    issued = Issued(comparison, tmp_path / "eval")
    packet, reason = assemble(comparison, issued)
    assert reason is None, reason
    assert packet["kind"] == "hold"
    assert packet["audit"]["independent_verdict_digest"] == issued.doc["verdict_digest"]
    assert packet["comparison"]["operating_point_digest"] == issued.doc["operating_point_digest"]
    assert packet["authority"] == {"release": False, "delivery": False}
    assert cp.verify_packet(
        packet,
        comparison,
        issued.receipt,
        issued.doc,
        issued.ctx,
        slice_key=key_for(comparison),
    )
    out, store = stage(comparison, issued, tmp_path / "stage")
    assert out.status == "staged" and out.packet == packet, out


def test_every_other_hold_still_applies_to_an_accepted_528_verdict(tmp_path):
    """Acceptance by the packet grants nothing: D11 and ``authorize`` still hold the gain."""
    assert iv.REGISTERED_POLICY_FLOOR is None
    comparison = named_run(tmp_path / "run", hold=True)
    issued = Issued(comparison, tmp_path / "eval")
    packet, reason = assemble(comparison, issued)
    assert reason is None and packet["kind"] == "hold"
    doc = issued.doc
    assert doc["alert_gates"]["policy_floor_hold"] == "policy_floor_unset"
    assert not any(doc["effects"].values())
    for effect in ("improvement_alert", "release_request", "handoff_request"):
        out = issued.ev.authorize(doc, effect=effect)
        assert out["authorized"] is False and out["reason"] == "policy_floor_unset", out
    # A copy the evaluator did not store is refused by ``authorize`` even though it digests.
    forged = reseal_verdict({**doc, "producer_id": "generated-other-worker"})
    assert cp._verdict_ok(forged)
    assert issued.ev.authorize(forged, effect="improvement_alert") == {
        "authorized": False,
        "effect": "improvement_alert",
        "reason": "verdict_document_mismatch",
        "state": "unverified",
    }


def test_improvement_path_still_refuses_a_528_verdict_and_never_as_predating(tmp_path):
    """The form no longer refuses, but the improvement path still holds: the 528 membership
    digest is domain-separated (``opentour.pilot.sealed-membership.v1``) while the comparison's
    is ``digest(sorted ids)``, so the denominator binding refuses first. Recorded contract gap;
    no reconciliation is invented here. Live staging also holds for unverified issuers."""
    comparison = named_run(tmp_path / "run")
    issued = Issued(comparison, tmp_path / "eval")
    assert issued.doc["verdict"] == "reproduced"
    for fixture in (False, True):
        packet, reason = assemble(comparison, issued, allow_generated_fixture=fixture)
        assert packet is None and reason == "denominator_changed", reason
    out, store = stage(comparison, issued, tmp_path / "stage")
    assert out.status == "refused" and out.reason == "denominator_changed", out
    assert empty(store)


# ------------------------------------------------------------------------------ adverse cases


def test_old_form_528_verdict_is_still_refused(tmp_path):
    comparison = named_run(tmp_path / "run", hold=True)
    issued = Issued(comparison, tmp_path / "eval")
    body = {k: v for k, v in issued.doc.items() if k != "operating_point_digest_form"}
    legacy = reseal_verdict(body)
    assert cp._verdict_ok(legacy)
    packet, reason = assemble(comparison, issued, legacy)
    assert packet is None and reason == "operating_point_digest_predates_slice_digests"
    out, store = stage(comparison, issued, tmp_path / "stage", legacy)
    assert out.reason == "operating_point_digest_predates_slice_digests" and empty(store)


def test_tampered_slice_digest_is_refused(tmp_path):
    comparison = named_run(tmp_path / "run", hold=True)
    terms = substituted(comparison)
    terms["critical_slices"] = [digest("generated-tampered-slice"), *terms["critical_slices"][1:]]
    # The evaluator sees a self-consistent operating point (format and digest both check)...
    issued = Issued(comparison, tmp_path / "eval", terms=terms)
    assert iv.operating_point_binding(issued.terms, issued.point) is None
    assert issued.doc["operating_point_digest_form"] == cp.OPERATING_POINT_DIGEST_FORM
    # ...but the packet recomputes from the comparison under the key and refuses it.
    packet, reason = assemble(comparison, issued)
    assert packet is None and reason == "verdict_binding_mismatch"
    # A digest edited inside an otherwise honest, resealed document is refused the same way.
    honest = Issued(comparison, tmp_path / "eval-honest")
    edited = reseal_verdict({**honest.doc, "operating_point_digest": issued.point})
    assert assemble(comparison, honest, edited) == (None, "verdict_binding_mismatch")
    # And a digest over the RAW terms is the same refusal, never a distinguishing code.
    raw = reseal_verdict({**honest.doc, "operating_point_digest": digest(comparison["terms"])})
    assert assemble(comparison, honest, raw) == (None, "verdict_binding_mismatch")


def test_verdict_from_a_different_slice_set_is_refused(tmp_path):
    comparison = named_run(tmp_path / "run", hold=True)
    other = named_run(tmp_path / "other", critical=SLICE_NAMES[:1], hold=True)
    assert other["terms"]["critical_slices"] != comparison["terms"]["critical_slices"]
    issued = Issued(comparison, tmp_path / "eval", terms=substituted(other))
    assert issued.doc["operating_point_digest"] == cp.operating_point_digest(substituted(other))
    assert assemble(comparison, issued) == (None, "verdict_binding_mismatch")
    # Same names under another engagement's key are a different slice-digest set too.
    moved = in_another_engagement(comparison)
    elsewhere = Issued(comparison, tmp_path / "eval-b", terms=substituted(moved))
    assert substituted(moved)["critical_slices"] != substituted(comparison)["critical_slices"]
    assert assemble(comparison, elsewhere) == (None, "verdict_binding_mismatch")
    # The evaluator's slice veto must gate exactly the bound set: gating another is never a
    # reproduced verdict, so the improvement path cannot use it.
    gated = Issued(
        comparison,
        tmp_path / "eval-g",
        critical=tuple(substituted(other)["critical_slices"]),
    )
    assert gated.doc["verdict"] == "not_reproduced"
    assert "critical_slices_not_bound_to_operating_point" in gated.doc["reasons"]
    # Material slices are part of the substituted set as well.
    material = named_run(
        tmp_path / "mat", critical=SLICE_NAMES[:1], hold=True, material=SLICE_NAMES[1:]
    )
    mixed = Issued(material, tmp_path / "eval-m", terms=substituted(other))
    assert assemble(material, mixed) == (None, "verdict_binding_mismatch")
    assert assemble(material, Issued(material, tmp_path / "eval-m2"))[1] is None


# ---------------------------------------------------------------------------- leak properties


def test_evaluator_refuses_unsubstituted_slice_names_and_publishes_none(tmp_path):
    """The evaluator never issues a document carrying a name it can tell is not substituted."""
    comparison = named_run(tmp_path / "run", hold=True)
    raw = copy.deepcopy(comparison["terms"])
    with pytest.raises(iv.VerdictInputError, match="operating_point_slice_names_not_substituted"):
        Issued(comparison, tmp_path / "eval", terms=raw)
    verdicts = tmp_path / "eval" / "evaluator-custody" / "verdicts"
    assert list(verdicts.iterdir()) == []


def test_no_slice_name_or_key_appears_in_the_528_verdict_or_the_packet(tmp_path):
    comparison = named_run(
        tmp_path / "run", hold=True, critical=SLICE_NAMES[:1], material=SLICE_NAMES[1:]
    )
    issued = Issued(comparison, tmp_path / "eval")
    packet, reason = assemble(comparison, issued)
    assert reason is None
    key = key_for(comparison)
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
