"""OPE-416 worker side: consumed-dependency manifest, seal, training parents, current reducer.

Fixture kind: generated sentinel (local). The "offline consumers" case runs the producer in a
fresh interpreter in which the OPE-544, OPE-525 and OPE-516 modules cannot be imported. None of
this is provider, installed-witness or customer evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import candidate_snapshot_fixture as fx
import pytest
from opentour_cell.pilot.consumed_dependencies import (
    propagate_retractions,
    to_lineage_manifest,
    to_recompute_families,
    to_seed_roots,
    union_synthetic,
)
from opentour_cell.pilot.learning_profile import ProfileError, parse_settings

HERE = Path(__file__).parent


def _setup(tmp_path: Path, moment: str | None = None) -> fx.World:
    w = fx.world(tmp_path, moment)
    w.store.commit(fx.settings(), principal=fx.CUSTODIAN)
    return w


def _example(w: fx.World, receipt: dict[str, Any], ticket: str) -> dict[str, Any]:
    snap = w.producer.snapshot(fx.TENANT_A, fx.WF, receipt["snapshot_digest"])
    return next(e for e in snap["examples"] if e["decision_id"] == ticket)


def _record(w: fx.World, digest: str, example: str, **over: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "purpose": "dataset_preparation",
        "consumer_kind": "dataset",
        "consumer_ref": "generated-dataset-1",
        "consumer_revision": "r1",
        "rule_revision": "generated-rule-1",
        "paths": ["features/amount", "features/gateway_status", "outcome/label"],
    }
    kwargs.update(over)
    return w.recorder.record(fx.TENANT_A, fx.WF, digest, example, **kwargs)


def _two_verified(w: fx.World) -> tuple[dict[str, Any], Any]:
    source = fx.pair(
        live=[fx.dec("T1", fx.at(10)), fx.dec("T2", fx.at(10, 1))],
        outcomes=[fx.cor("c-1", "T1", fx.at(10, 5)), fx.cor("c-2", "T2", fx.at(10, 6))],
    )
    return w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(source)), source


# --------------------------------------------------------------------------- acceptance 6


def test_a_later_settlement_cannot_alter_the_inputs_the_original_decision_saw(
    tmp_path: Path,
) -> None:
    """Refund fixture: timeout at 10:00 stays pending; settlement at 10:15 is not an input."""
    w = _setup(tmp_path, fx.at(10, 5))
    decision = fx.dec("REF-1", fx.at(10), action="REVIEW", gateway="timeout")
    early = fx.pair(live=[decision], captured=fx.at(10, 5))
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "at-1005", fx.reader(early))
    pending = _example(w, first, "REF-1")
    assert (pending["state"], pending["reason"]) == ("pending", "awaiting_outcome")

    record = _record(
        w,
        first["snapshot_digest"],
        pending["example_ref"],
        purpose="evaluation",
        consumer_kind="simple_rule",
        consumer_ref="generated-rule",
        paths=[
            "features/amount",
            "features/gateway_status",
            "outcome/label",
        ],
    )
    body = record["body"]
    assert [c["path"] for c in body["consumed"]] == ["features/amount", "features/gateway_status"]
    assert all(c["available_at"] <= fx.at(10) for c in body["consumed"])
    # No outcome existed at consumption: the label is an unknown link, never a guessed edge.
    assert body["label_inputs"] == [] and body["unknown_dependencies"] == ["outcome/label"]
    assert body["state_at_consumption"] == "pending" and body["outcome_time"] is None
    records = tmp_path / "dependencies" / fx.TENANT_A / fx.WF / "records"
    original = {p.name: p.read_bytes() for p in records.iterdir()}
    snapshots = tmp_path / "snapshots" / fx.TENANT_A / fx.WF / "snapshots"
    first_snapshot = (snapshots / "snap-00000001.json").read_bytes()

    w.clock.set(fx.at(10, 30))
    settled = fx.pair(
        live=[decision],
        outcomes=[fx.cor("c-settle", "REF-1", fx.at(10, 15), value="REFUND_SETTLED")],
        captured=fx.at(10, 30),
    )
    second = w.producer.trigger(fx.TENANT_A, fx.WF, "at-1030", fx.reader(settled))
    verified = _example(w, second, "REF-1")
    assert verified["state"] == "verified"
    assert verified["correction"]["outcome_time"] == fx.at(10, 15)
    # The decision's inputs are the same fields and versions; the settlement is not one.
    assert verified["inputs"] == pending["inputs"]
    assert verified["inputs_digest"] == pending["inputs_digest"]

    relation = w.recorder.attach_outcome(
        fx.TENANT_A, fx.WF, record["record_ref"], second["snapshot_digest"]
    )
    assert relation["relationship_version"] == 1 and relation["available_at"] == fx.at(10, 15)
    assert {p.name: p.read_bytes() for p in records.iterdir()} == original
    assert (snapshots / "snap-00000001.json").read_bytes() == first_snapshot
    # The original snapshot still shows what was known at 10:05.
    still = _example(w, first, "REF-1")
    assert still["state"] == "pending"
    with pytest.raises(ProfileError, match="outcome_not_verified"):
        w.recorder.attach_outcome(
            fx.TENANT_A, fx.WF, record["record_ref"], first["snapshot_digest"]
        )

    # A source that rewrites the 10:00 decision with the settlement is disputed, not absorbed.
    rewritten = fx.dec("REF-1", fx.at(10), action="REVIEW", gateway="settled")
    w.clock.set(fx.at(10, 40))
    forked = fx.pair(
        live=[decision, rewritten],
        outcomes=[fx.cor("c-settle", "REF-1", fx.at(10, 15), value="REFUND_SETTLED")],
        captured=fx.at(10, 40),
    )
    third = w.producer.trigger(fx.TENANT_A, fx.WF, "at-1040", fx.reader(forked))
    assert _example(w, third, "REF-1")["reason"] == "contradictory_decision"


def test_a_consumer_cannot_record_a_label_for_a_protected_or_ineligible_example(
    tmp_path: Path,
) -> None:
    w = _setup(tmp_path)
    source = fx.pair(
        live=[fx.dec("P1", fx.at(10), customer=fx.HELD_OUT), fx.dec("Q1", fx.at(10))],
        outcomes=[fx.cor("c-1", "P1", fx.at(10, 5))],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    protected = _example(w, receipt, "P1")["example_ref"]
    pending = _example(w, receipt, "Q1")["example_ref"]
    with pytest.raises(ProfileError, match="example_not_eligible"):
        _record(w, receipt["snapshot_digest"], protected)
    with pytest.raises(ProfileError, match="protected_evaluation_only"):
        _record(w, receipt["snapshot_digest"], protected, purpose="simple_rule")
    with pytest.raises(ProfileError, match="example_not_eligible"):
        _record(w, receipt["snapshot_digest"], pending)
    with pytest.raises(ProfileError, match="unknown_purpose"):
        _record(w, receipt["snapshot_digest"], pending, purpose="training")
    with pytest.raises(ProfileError, match="unknown_consumer"):
        _record(
            w, receipt["snapshot_digest"], pending, consumer_kind="oracle", purpose="evaluation"
        )


# --------------------------------------------------------------------------- acceptance 7

_RESEAL = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[2])
    import candidate_snapshot_fixture as fx
    w = fx.world(Path(sys.argv[1]))
    sealed = w.recorder.seal(fx.TENANT_A, fx.WF, sys.argv[3])
    paths = sorted(p for f in sealed["forward"] for r in f["records"] for p in r["paths"])
    print(json.dumps({"digest": sealed["seal_digest"], "paths": paths}))
    """
)


def test_resealing_after_a_restart_gives_the_same_redacted_digest_and_actual_paths(
    tmp_path: Path,
) -> None:
    w = _setup(tmp_path)
    receipt, _ = _two_verified(w)
    digest = receipt["snapshot_digest"]
    for ticket in ("T1", "T2"):
        _record(w, digest, _example(w, receipt, ticket)["example_ref"])
    sealed = w.recorder.seal(fx.TENANT_A, fx.WF, digest)
    paths = sorted(p for f in sealed["forward"] for r in f["records"] for p in r["paths"])
    assert paths == sorted(["features/amount", "features/gateway_status", "outcome/label"] * 2)
    assert sealed["holds"] == []
    text = json.dumps(sealed)
    assert "REFUND" not in text and fx.PROTECTED_SENTINEL not in text and "amt " not in text

    results = []
    for seed in ("0", "7"):
        done = subprocess.run(
            [sys.executable, "-c", _RESEAL, str(tmp_path), str(HERE), digest],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        results.append(json.loads(done.stdout))
    assert results == [{"digest": sealed["seal_digest"], "paths": paths}] * 2

    # A later dataset record cannot change a sealed snapshot's manifest.
    with pytest.raises(ProfileError, match="snapshot_sealed"):
        _record(w, digest, _example(w, receipt, "T1")["example_ref"], consumer_ref="other")


def test_a_tampered_consumption_record_is_refused_at_seal(tmp_path: Path) -> None:
    w = _setup(tmp_path)
    receipt, _ = _two_verified(w)
    stored = _record(w, receipt["snapshot_digest"], _example(w, receipt, "T1")["example_ref"])
    folder = tmp_path / "dependencies" / fx.TENANT_A / fx.WF / "records"
    path = folder / f"{stored['record_ref']}.json"
    path.write_text(path.read_text().replace("features/amount", "features/amounT"))
    with pytest.raises(ProfileError, match="dependency_tampered"):
        w.recorder.seal(fx.TENANT_A, fx.WF, receipt["snapshot_digest"])


def test_a_forged_record_under_the_same_reference_conflicts_on_replay(tmp_path: Path) -> None:
    from opentour_cell.pilot.learning_profile import _pin
    from opentour_cell.pilot.operating_point import canonical

    w = _setup(tmp_path)
    receipt, _ = _two_verified(w)
    example = _example(w, receipt, "T1")["example_ref"]
    stored = _record(w, receipt["snapshot_digest"], example)
    folder = tmp_path / "dependencies" / fx.TENANT_A / fx.WF / "records"
    forged = json.loads((folder / f"{stored['record_ref']}.json").read_text())
    forged["body"]["consumed"] = []
    body = canonical(forged)
    (folder / f"{stored['record_ref']}.json").write_text(body)
    (folder / f"{stored['record_ref']}.pin").write_text(_pin(body))
    with pytest.raises(ProfileError, match="dependency_conflict"):
        _record(w, receipt["snapshot_digest"], example)


def test_an_identical_consumption_replay_returns_the_same_record(tmp_path: Path) -> None:
    w = _setup(tmp_path)
    receipt, _ = _two_verified(w)
    example = _example(w, receipt, "T1")["example_ref"]
    first = _record(w, receipt["snapshot_digest"], example)
    w.clock.set(fx.at(13))
    assert _record(w, receipt["snapshot_digest"], example) == first
    assert len(w.recorder.records(fx.TENANT_A, fx.WF)) == 1


# --------------------------------------------------------------------------- acceptance 10


def test_reopen_or_withdrawal_retracts_future_training_and_stales_proposals_only(
    tmp_path: Path,
) -> None:
    w = _setup(tmp_path)
    live = [fx.dec(t, fx.at(10, n)) for n, t in enumerate(("K1", "K2", "K3"))]
    verified = [fx.cor(f"c-{t}", t, fx.at(10, 5)) for t in ("K1", "K2", "K3")]
    receipt = w.producer.trigger(
        fx.TENANT_A, fx.WF, "t1", fx.reader(fx.pair(live=live, outcomes=verified))
    )
    digest = receipt["snapshot_digest"]
    refs = {t: _example(w, receipt, t)["example_ref"] for t in ("K1", "K2", "K3")}
    for n, ticket in enumerate(("K1", "K2", "K3")):
        _record(w, digest, refs[ticket], artifacts={"proposal_ref": f"generated-prop-{n + 1}"})
    snapshots = tmp_path / "snapshots" / fx.TENANT_A / fx.WF / "snapshots"
    as_of_bytes = (snapshots / "snap-00000001.json").read_bytes()

    w.clock.set(fx.at(11))
    later = fx.pair(
        live=live,
        outcomes=[
            *verified,
            fx.cor("c-K1-2", "K1", fx.at(10, 30), ver=2, status="reopened", value=None),
            fx.cor("c-K2-2", "K2", fx.at(10, 40), ver=2, status="withdrawn", value=None),
        ],
        captured=fx.at(11),
    )
    current = w.producer.current(
        fx.TENANT_A,
        fx.WF,
        digest,
        fx.reader(later),
        w.recorder.proposals(fx.TENANT_A, fx.WF, digest),
    )
    assert current["retracted"] == sorted(
        [
            {"example_ref": refs["K1"], "reason": "reopened"},
            {"example_ref": refs["K2"], "reason": "correction_withdrawn"},
        ],
        key=lambda r: r["example_ref"],
    )
    assert current["stale_proposals"] == ["generated-prop-1", "generated-prop-2"]
    assert current["eligible"] == [refs["K3"]]
    assert current["counters"]["live"]["verified"] == 1  # the visible current denominator moved
    # The as-of report the reviewer inspected is unchanged.
    assert (snapshots / "snap-00000001.json").read_bytes() == as_of_bytes
    as_of = w.producer.snapshot(fx.TENANT_A, fx.WF, digest)
    assert as_of["counters"]["eligible"] == 3

    parents = w.recorder.training_parents(fx.TENANT_A, fx.WF, digest, fx.reader(later))
    assert [p["example_ref"] for p in parents["parents"]] == [refs["K3"]]
    assert parents["counts"] == {
        "parents": 1,
        "independent_root_families": 1,
        "verified_observed": 1,
    }
    assert parents["stale_proposals"] == ["generated-prop-1", "generated-prop-2"]

    from opentour_cell.pilot.training_variants import VariantLedger

    ledger = VariantLedger(tmp_path / "variants.sqlite")
    invalidated = propagate_retractions(ledger, parents, as_of)
    assert set(invalidated) == {refs["K1"], refs["K2"]}
    import sqlite3

    with sqlite3.connect(tmp_path / "variants.sqlite") as db:
        states = dict(db.execute("SELECT root_ref, state FROM roots").fetchall())
    assert states == {refs["K1"]: "retracted", refs["K2"]: "retracted"}


def test_synthetic_children_cannot_change_live_counts(tmp_path: Path) -> None:
    w = _setup(tmp_path)
    receipt, source = _two_verified(w)
    digest = receipt["snapshot_digest"]
    refs = [_example(w, receipt, t)["example_ref"] for t in ("T1", "T2")]
    for ref in refs:
        _record(w, digest, ref)
    parents = w.recorder.training_parents(fx.TENANT_A, fx.WF, digest, fx.reader(source))
    children = [{"child_ref": f"var-{n}", "root_ref": refs[0]} for n in range(100)]
    children.append({"child_ref": "var-orphan", "root_ref": "ex-not-a-parent"})
    union = union_synthetic(parents, children)
    assert (
        union["live_counts"]
        == parents["counts"]
        == {
            "parents": 2,
            "independent_root_families": 2,
            "verified_observed": 2,
        }
    )
    assert union["synthetic_children"] == 100 and union["rows"] == 102
    assert union["rejected"] == [{"child_ref": "var-orphan", "reason": "child_without_live_parent"}]


# --------------------------------------------------------------------------- acceptance 11

_OFFLINE = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    OFFLINE = (
        "opentour_cell.pilot.consumption_lineage",
        "opentour_cell.pilot.incremental_recompute",
        "opentour_cell.pilot.training_variants",
    )
    for name in OFFLINE:
        sys.modules[name] = None  # importing any of them now raises ImportError
    sys.path.insert(0, sys.argv[2])
    import candidate_snapshot_fixture as fx
    w = fx.world(Path(sys.argv[1]))
    w.store.commit(fx.settings(), principal=fx.CUSTODIAN)
    source = fx.pair(
        live=[fx.dec("H1", fx.at(10)), fx.dec("H2", fx.at(10, 1))],
        outcomes=[fx.cor("c-1", "H1", fx.at(10, 5)), fx.cor("c-2", "H2", fx.at(10, 6))],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "historical-1", fx.reader(source))
    snap = w.producer.snapshot(fx.TENANT_A, fx.WF, receipt["snapshot_digest"])
    refs = {e["decision_id"]: e["example_ref"] for e in snap["examples"]}
    w.recorder.record(
        fx.TENANT_A, fx.WF, receipt["snapshot_digest"], refs["H1"],
        purpose="dataset_preparation", consumer_kind="dataset", consumer_ref="generated-ds",
        consumer_revision="r1", rule_revision="generated-rule-1",
        paths=["features/amount", "outcome/label"],
    )
    parents = w.recorder.training_parents(
        fx.TENANT_A, fx.WF, receipt["snapshot_digest"], fx.reader(source)
    )
    sealed = w.recorder.seal(fx.TENANT_A, fx.WF, receipt["snapshot_digest"])
    print(json.dumps({
        "refs": refs, "holds": sealed["holds"], "forward": sealed["forward"],
        "parents": [p["example_ref"] for p in parents["parents"]],
        "loaded": sorted(n for n in OFFLINE if sys.modules.get(n) is not None),
        "receipt": receipt,
    }))
    """
)


def test_with_all_three_consumers_offline_missing_evidence_produces_a_scoped_hold(
    tmp_path: Path,
) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _OFFLINE, str(tmp_path), str(HERE)],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(done.stdout)
    assert out["loaded"] == []
    assert out["receipt"]["state"] == "committed"
    h1, h2 = out["refs"]["H1"], out["refs"]["H2"]
    forward = {f["example_ref"]: f for f in out["forward"]}
    assert forward[h1]["records"][0]["paths"] == ["features/amount", "outcome/label"]
    assert forward[h2]["records"] == []
    (hold,) = out["holds"]
    assert (hold["example_ref"], hold["reason"], hold["scope"]) == (
        h2,
        "consumption_record_missing",
        "root_family",
    )
    # The hold is scoped: the recorded family still yields a training parent.
    assert out["parents"] == [h1]


def test_an_unknown_dependency_holds_only_its_own_root_family(tmp_path: Path) -> None:
    w = _setup(tmp_path)
    receipt, source = _two_verified(w)
    digest = receipt["snapshot_digest"]
    t1, t2 = (_example(w, receipt, t)["example_ref"] for t in ("T1", "T2"))
    _record(w, digest, t1, paths=["features/amount", "features/settlement_status"])
    _record(w, digest, t2)
    sealed = w.recorder.seal(fx.TENANT_A, fx.WF, digest)
    assert [(h["example_ref"], h["reason"]) for h in sealed["holds"]] == [
        (t1, "unknown_dependency")
    ]
    assert sealed["unknown_dependencies"] == ["features/settlement_status"]
    parents = w.recorder.training_parents(fx.TENANT_A, fx.WF, digest, fx.reader(source))
    assert [p["example_ref"] for p in parents["parents"]] == [t2]


def test_protected_roots_expose_aggregate_status_only(tmp_path: Path) -> None:
    w = _setup(tmp_path)
    source = fx.pair(
        live=[fx.dec("P1", fx.at(10), customer=fx.HELD_OUT), fx.dec("D1", fx.at(10))],
        outcomes=[fx.cor("c-p", "P1", fx.at(10, 5)), fx.cor("c-d", "D1", fx.at(10, 5))],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    protected = _example(w, receipt, "P1")
    w.recorder.record(
        fx.TENANT_A,
        fx.WF,
        receipt["snapshot_digest"],
        protected["example_ref"],
        purpose="evaluation",
        consumer_kind="comparator",
        consumer_ref="generated-cmp",
        consumer_revision="r1",
        rule_revision="generated-rule-1",
        paths=["features/amount"],
    )
    sealed = w.recorder.seal(fx.TENANT_A, fx.WF, receipt["snapshot_digest"])
    assert set(sealed["partitions"]["protected"]) == {"digest", "status", "examples", "groups"}
    assert sealed["partitions"]["protected"]["examples"] == 1
    text = json.dumps(sealed)
    assert protected["example_ref"] not in text and protected["group"] not in text


# --------------------------------------------------------------------------- consumer projections


def test_the_ope544_reader_resolves_a_claim_from_a_projected_record(tmp_path: Path) -> None:
    from opentour_cell.pilot import consumption_lineage as cl

    w = _setup(tmp_path)
    receipt, _ = _two_verified(w)
    stored = _record(
        w,
        receipt["snapshot_digest"],
        _example(w, receipt, "T1")["example_ref"],
        paths=["features/amount", "outcome/label"],
    )
    manifest = to_lineage_manifest(
        w.recorder,
        fx.TENANT_A,
        fx.WF,
        stored["record_ref"],
        rule_kind="simple_rule",
        result_ref="generated-result-1",
        candidate_ref="generated-cand-1",
        claim_refs=["generated-claim-1"],
    )
    store = cl.ManifestStore(tmp_path / "lineage")
    store.put(manifest)
    owner = cl.Actor(fx.TENANT_A, "generated-owner-1", cl.OWNER)
    grant = cl.GrantState(
        fx.TENANT_A, fx.WF, 3, False, "2099-01-01T00:00:00Z", {"generated-owner-1": cl.OWNER}
    )
    reader = cl.ConsumptionReader(store, lambda tenant, wf: grant)
    resolved = reader.resolve_claim(
        cl.Request(owner, fx.WF, 3, "2026-01-02T00:00:00Z"), "generated-claim-1"
    )
    assert isinstance(resolved, cl.Resolution) and resolved.status == "resolved"
    assert [link.path for link in resolved.inputs] == ["features/amount"]

    held = _record(
        w,
        receipt["snapshot_digest"],
        _example(w, receipt, "T2")["example_ref"],
        paths=["features/amount", "features/unlisted"],
    )
    doubtful = to_lineage_manifest(
        w.recorder,
        fx.TENANT_A,
        fx.WF,
        held["record_ref"],
        rule_kind="simple_rule",
        result_ref="generated-result-2",
        candidate_ref="generated-cand-2",
        claim_refs=["generated-claim-2"],
    )
    store.put(doubtful)
    again = reader.resolve_claim(
        cl.Request(owner, fx.WF, 3, "2026-01-02T00:00:00Z"), "generated-claim-2"
    )
    assert isinstance(again, cl.Resolution) and again.status == "hold"


def test_the_ope525_registry_accepts_the_projected_families(tmp_path: Path) -> None:
    from opentour_cell.pilot.incremental_recompute import ChangeEvent, Registry

    w = _setup(tmp_path)
    receipt, _ = _two_verified(w)
    digest = receipt["snapshot_digest"]
    t1, t2 = (_example(w, receipt, t)["example_ref"] for t in ("T1", "T2"))
    _record(w, digest, t1, paths=["features/amount", "outcome/label"])
    _record(w, digest, t2, paths=["features/gateway_status", "outcome/label", "features/x"])
    families = to_recompute_families(w.recorder.seal(fx.TENANT_A, fx.WF, digest))
    registry = Registry(families)
    assert {f.lineage_complete for f in families} == {True, False}
    # Each path keeps its own source revision; the label is not a decision-time input.
    assert {(r.source_ref, r.field_path) for f in families for r in f.consumes} == {
        (f"{fx.LIVE}@1", "features/amount"),
        (f"{fx.LIVE}@1", "features/gateway_status"),
    }
    change = ChangeEvent("e1", 2, "upsert", f"{fx.LIVE}@1", "features/amount")
    (dirty,) = registry.lookup(change, "development")
    assert dirty == _example(w, receipt, "T1")["group"]


def test_the_ope516_seed_projection_rereads_values_and_refuses_a_changed_label(
    tmp_path: Path,
) -> None:
    w = _setup(tmp_path)
    receipt, source = _two_verified(w)
    digest = receipt["snapshot_digest"]
    for ticket in ("T1", "T2"):
        _record(w, digest, _example(w, receipt, ticket)["example_ref"])
    parents = w.recorder.training_parents(fx.TENANT_A, fx.WF, digest, fx.reader(source))
    sealed = w.recorder.seal(fx.TENANT_A, fx.WF, digest)
    settings = parse_settings(fx.settings())
    roots, seal = to_seed_roots(parents, sealed, settings, source)
    assert [r.target for r in roots] == ["REFUND", "REFUND"]
    assert {r.split for r in roots} == {"training"} and all(r.target_verified for r in roots)
    assert "customer_age" not in roots[0].fields
    assert set(seal.assignments.values()) == {("training",)}

    swapped = fx.pair(
        live=[fx.dec("T1", fx.at(10)), fx.dec("T2", fx.at(10, 1))],
        outcomes=[
            fx.cor("c-1", "T1", fx.at(10, 5), value="DENY"),
            fx.cor("c-2", "T2", fx.at(10, 6)),
        ],
    )
    with pytest.raises(ProfileError, match="seed_label_changed"):
        to_seed_roots(parents, sealed, settings, swapped)


def test_later_artifacts_attach_as_versioned_caller_asserted_relations(tmp_path: Path) -> None:
    w = _setup(tmp_path)
    receipt, _ = _two_verified(w)
    stored = _record(w, receipt["snapshot_digest"], _example(w, receipt, "T1")["example_ref"])
    body = stored["body"]
    assert body["caller_asserted"]["consumer"]["ref"] == "generated-dataset-1"
    assert body["verifier"] == {"actor_id": fx.VERIFIER, "settings_revision": 1}
    first = w.recorder.attach_artifact(
        fx.TENANT_A, fx.WF, stored["record_ref"], "frozen_result", "generated-result-1"
    )
    again = w.recorder.attach_artifact(
        fx.TENANT_A, fx.WF, stored["record_ref"], "frozen_result", "generated-result-1"
    )
    second = w.recorder.attach_artifact(
        fx.TENANT_A, fx.WF, stored["record_ref"], "proposal", "generated-prop-9"
    )
    assert first == again and first["basis"] == {"caller_asserted": True}
    assert (first["relationship_version"], second["relationship_version"]) == (1, 2)
    with pytest.raises(ProfileError, match="unknown_relationship"):
        w.recorder.attach_artifact(fx.TENANT_A, fx.WF, stored["record_ref"], "outcome", "x-1")
    with pytest.raises(ProfileError, match="dependency_unknown"):
        w.recorder.attach_artifact(fx.TENANT_A, fx.WF, "dep-missing", "proposal", "p-1")
