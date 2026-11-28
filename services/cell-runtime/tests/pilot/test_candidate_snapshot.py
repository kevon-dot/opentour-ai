"""OPE-416 worker side: settings batch, source-pair joins, as-of snapshot, receipt.

Fixture kind: generated sentinel (local). These tests prove software behaviour only. They are
not provider, installed-Zap, Slack or customer evidence, and the receipt digest is
``digest_self_consistent_only``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import candidate_snapshot_fixture as fx
import pytest
from opentour_cell.pilot.candidate_snapshot import (
    REASON_COUNTER,
    REASONS,
    RECEIPT_FIELDS,
    SnapshotHold,
)
from opentour_cell.pilot.candidate_snapshot import (
    receipt as make_receipt,
)
from opentour_cell.pilot.learning_profile import (
    COUNTERS,
    Principal,
    ProfileError,
    parse_settings,
    parse_time,
)

FIXTURE = Path(__file__).parent / "fixtures" / "candidate-snapshot-v1.json"
DOC = Path(__file__).parents[2] / "docs" / "pilot-candidate-snapshot.md"


def _committed(w: fx.World, tenant: str = fx.TENANT_A, **over: Any) -> None:
    w.store.commit(fx.settings(tenant, **over), principal=fx.CUSTODIAN)


def _snapshot(w: fx.World, receipt: dict[str, Any], tenant: str = fx.TENANT_A) -> dict[str, Any]:
    return w.producer.snapshot(tenant, fx.WF, receipt["snapshot_digest"])


def _by_ticket(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["decision_id"]: e for e in snapshot["examples"]}


def _walk(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [str(k), *_walk(v)]]
    if isinstance(value, list):
        return [s for v in value for s in _walk(v)]
    return [str(value)]


# --------------------------------------------------------------------------- acceptance 1


def test_two_tenants_with_identical_case_ids_and_disjoint_references_stay_tenant_local(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w, fx.TENANT_A)
    _committed(w, fx.TENANT_B)
    rows = [fx.dec("T1", fx.at(10)), fx.dec("T2", fx.at(10, 5))]
    a = fx.pair(fx.TENANT_A, live=rows, outcomes=[fx.cor("a-1", "T1", fx.at(10, 30))])
    b = fx.pair(
        fx.TENANT_B,
        live=rows,
        outcomes=[fx.cor("b-1", "T2", fx.at(10, 40), value="PARTIAL")],
    )
    ra = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(a))
    rb = w.producer.trigger(fx.TENANT_B, fx.WF, "trigger-1", fx.reader(b))
    sa, sb = _snapshot(w, ra, fx.TENANT_A), _snapshot(w, rb, fx.TENANT_B)

    # Same case IDs, but each tenant's correction verifies only its own decision.
    assert {k: e["state"] for k, e in _by_ticket(sa).items()} == {"T1": "verified", "T2": "pending"}
    assert {k: e["state"] for k, e in _by_ticket(sb).items()} == {"T1": "pending", "T2": "verified"}
    refs_a = {e["example_ref"] for e in sa["examples"]}
    refs_b = {e["example_ref"] for e in sb["examples"]}
    assert refs_a.isdisjoint(refs_b)
    assert ra["snapshot_digest"] != rb["snapshot_digest"]
    assert fx.TENANT_B not in json.dumps(sa) and fx.TENANT_A not in json.dumps(sb)
    # A tenant cannot reach another tenant's snapshot by digest.
    with pytest.raises(ProfileError, match="snapshot_unknown"):
        w.producer.snapshot(fx.TENANT_A, fx.WF, rb["snapshot_digest"])


def test_a_capture_bound_to_another_tenant_is_held_not_joined(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w, fx.TENANT_A)
    foreign = fx.pair(fx.TENANT_B, live=[fx.dec("T1", fx.at(10))])
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(foreign))
    assert receipt["state"] == "held" and receipt["reason"] == "foreign_tenant_capture"
    assert w.producer.snapshots(fx.TENANT_A, fx.WF) == []


# --------------------------------------------------------------------------- acceptance 2


def test_duplicate_contradictory_late_and_unattested_corrections_are_explicit_exclusions(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(
        live=[fx.dec(t, fx.at(10)) for t in ("DUP", "CONTRA", "LATE", "NOBODY", "STRANGER")],
        outcomes=[
            fx.cor("c-dup-1", "DUP", fx.at(10, 5)),
            fx.cor("c-dup-2", "DUP", fx.at(10, 5)),
            fx.cor("c-contra-1", "CONTRA", fx.at(10, 5), value="REFUND"),
            fx.cor("c-contra-2", "CONTRA", fx.at(10, 6), value="PARTIAL"),
            fx.cor("c-late-1", "LATE", fx.at(10, 5)),
            # v2 changes a final that had already matured (10:15) without a reopen.
            fx.cor("c-late-2", "LATE", fx.at(11), value="PARTIAL", ver=2),
            fx.cor("c-nobody", "NOBODY", fx.at(10, 5), by=None),
            fx.cor("c-stranger", "STRANGER", fx.at(10, 5), by="generated-stranger"),
        ],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(source))
    snap = _snapshot(w, receipt)
    states = {k: (e["state"], e["reason"]) for k, e in _by_ticket(snap).items()}
    assert states == {
        "DUP": ("verified", None),
        "CONTRA": ("disputed", "contradictory_corrections"),
        "LATE": ("pending", "late_correction_revision"),
        "NOBODY": ("pending", "unattested_label"),
        "STRANGER": ("pending", "unattested_label"),
    }
    assert [x["reason"] for x in snap["exclusions"]] == ["duplicate_correction"]
    assert snap["reason_codes"] == {
        "contradictory_corrections": 1,
        "duplicate_correction": 1,
        "late_correction_revision": 1,
        "unattested_label": 2,
    }
    assert {k: snap["counters"][k] for k in COUNTERS} == {
        "eligible": 1,
        "pending": 3,
        "quarantined": 0,
        "disputed": 1,
    }
    # Every non-zero cell is below the default threshold of 5, so the receipt shows "<k".
    assert receipt["reason_codes"] == dict.fromkeys(snap["reason_codes"], "<k")
    assert receipt["counters"] == {
        "eligible": "<k",
        "pending": "<k",
        "quarantined": 0,
        "disputed": "<k",
    }


@pytest.mark.parametrize("how", ["model_answer", "ticket_close", "thumbs_up", "repeated_reply"])
def test_a_model_answer_close_thumbs_up_or_repeated_reply_never_verifies(
    tmp_path: Path, how: str
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    outcomes = [fx.cor("c-first", "T1", fx.at(10, 5), how=how)]
    if how == "repeated_reply":
        outcomes.append(fx.cor("c-again", "T1", fx.at(10, 6), how=how, ver=2, by=fx.VERIFIER))
    source = fx.pair(live=[fx.dec("T1", fx.at(10))], outcomes=outcomes)
    snap = _snapshot(w, w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source)))
    (only,) = snap["examples"]
    assert (only["state"], only["reason"], only["eligible"]) == (
        "pending",
        "non_verifying_provenance",
        False,
    )
    assert snap["counters"]["live"]["verified"] == 0


def test_contradictory_decision_deliveries_are_disputed_and_identical_ones_redundant(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(
        live=[
            fx.dec("SAME", fx.at(10)),
            fx.dec("SAME", fx.at(10)),
            fx.dec("FORK", fx.at(10), action="DENY"),
            fx.dec("FORK", fx.at(10), action="REFUND"),
        ],
        outcomes=[fx.cor("c-same", "SAME", fx.at(10, 5)), fx.cor("c-fork", "FORK", fx.at(10, 5))],
    )
    snap = _snapshot(w, w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source)))
    states = {k: (e["state"], e["reason"]) for k, e in _by_ticket(snap).items()}
    assert states == {"SAME": ("verified", None), "FORK": ("disputed", "contradictory_decision")}
    assert snap["counters"]["live"]["decisions"] == 2
    assert [x["reason"] for x in snap["exclusions"]] == ["redundant_delivery"]


def test_disputed_source_rows_are_excluded_from_training_and_verified_counts(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(
        live=[fx.dec("D1", fx.at(10), state="disputed"), fx.dec("D2", fx.at(10))],
        outcomes=[
            fx.cor("c-1", "D1", fx.at(10, 5)),
            fx.cor("c-2", "D2", fx.at(10, 5), state="disputed"),
        ],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    snap = _snapshot(w, receipt)
    for example in snap["examples"]:
        assert (example["state"], example["reason"], example["eligible"]) == (
            "disputed",
            "source_disputed",
            False,
        )
    assert snap["counters"]["live"] == {
        "cases": 2,
        "decisions": 2,
        "verified": 0,
        "pending": 0,
        "disputed": 2,
    }
    assert snap["counters"]["disputed"] == 2 and snap["counters"]["eligible"] == 0
    assert receipt["counters"]["disputed"] == "<k" and receipt["counters"]["eligible"] == 0
    with pytest.raises(ProfileError, match="disputed_not_consumable"):
        w.recorder.record(
            fx.TENANT_A,
            fx.WF,
            receipt["snapshot_digest"],
            snap["examples"][0]["example_ref"],
            purpose="evaluation",
            consumer_kind="comparator",
            consumer_ref="cmp-1",
            consumer_revision="r1",
            rule_revision="rule-1",
            paths=["features/amount"],
        )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"state": "maybe"}, "unknown_row_state"),
        ({"ticket_rev": True}, "malformed_decision"),
        ({"ticket_rev": "1"}, "malformed_decision"),
        ({"case_no": 7}, "malformed_decision"),
        ({"gateway": 7}, "malformed_decision"),
        ({"inputs_at": fx.at(10, 1)}, "future_decision_features"),
    ],
)
def test_malformed_or_unattributable_decisions_are_quarantined_and_count_zero(
    tmp_path: Path, override: dict[str, Any], reason: str
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("Q1", fx.at(10), **override)])
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    snap = _snapshot(w, receipt)
    assert snap["examples"] == []
    assert [q["reason"] for q in snap["quarantined"]] == [reason]
    assert snap["counters"]["quarantined"] == 1
    assert receipt["counters"]["quarantined"] == "<k"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"how": "gut_feeling"}, "unknown_provenance"),
        ({"status": "approved"}, "unknown_correction_status"),
        ({"ver": True}, "malformed_correction"),
        ({"case_no": "case-OTHER"}, "ambiguous_join"),
    ],
)
def test_corrections_with_unknown_values_or_ambiguous_joins_fail_closed(
    tmp_path: Path, override: dict[str, Any], reason: str
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(
        live=[fx.dec("J1", fx.at(10))], outcomes=[fx.cor("c-1", "J1", fx.at(10, 5), **override)]
    )
    snap = _snapshot(w, w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source)))
    assert [q["reason"] for q in snap["quarantined"]] == [reason]
    assert _by_ticket(snap)["J1"]["state"] == "pending"


def test_a_correction_naming_a_decision_in_two_namespaces_is_an_ambiguous_join(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(
        live=[fx.dec("BOTH", fx.at(10))],
        starter=[fx.dec("BOTH", fx.at(10))],
        outcomes=[fx.cor("c-1", "BOTH", fx.at(10, 5)), fx.cor("c-2", "NOPE", fx.at(10, 5))],
    )
    snap = _snapshot(w, w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source)))
    assert [q["reason"] for q in snap["quarantined"]] == ["ambiguous_join"]
    assert [x["reason"] for x in snap["exclusions"]] == ["orphan_correction"]
    assert snap["counters"]["awaiting_decision"] == 1
    assert {e["state"] for e in snap["examples"]} == {"pending"}


@pytest.mark.parametrize(
    ("decisions", "outcomes", "expected", "excluded"),
    [
        (  # one correction ID delivered twice with different content
            [fx.dec("G1", fx.at(10))],
            [fx.cor("c-1", "G1", fx.at(10, 5)), fx.cor("c-1", "G1", fx.at(10, 5), value="DENY")],
            ("disputed", "correction_id_conflict"),
            [],
        ),
        (  # an outcome timestamped before the decision it claims to settle
            [fx.dec("G1", fx.at(10))],
            [fx.cor("c-1", "G1", fx.at(9, 55))],
            ("pending", "outcome_before_decision"),
            [],
        ),
        (  # a second correction version with no first version
            [fx.dec("G1", fx.at(10))],
            [fx.cor("c-2", "G1", fx.at(10, 5), ver=2)],
            ("pending", "missing_correction_predecessor"),
            [],
        ),
        (  # a second decision revision with no first revision
            [fx.dec("G1", fx.at(10), rev=2)],
            [fx.cor("c-1", "G1", fx.at(10, 5), rev=2)],
            ("pending", "missing_decision_predecessor"),
            [],
        ),
        (  # a decision revision that moves the decision to another case
            [fx.dec("G1", fx.at(10)), fx.dec("G1", fx.at(10, 1), rev=2, case="case-other")],
            [],
            ("disputed", "decision_case_changed"),
            [],
        ),
        (  # an outcome for an earlier decision revision is stale, not current truth
            [fx.dec("G1", fx.at(10)), fx.dec("G1", fx.at(10, 1), rev=2)],
            [fx.cor("c-1", "G1", fx.at(10, 5)), fx.cor("c-2", "G1", fx.at(10, 6), rev=2)],
            ("verified", None),
            ["stale_revision"],
        ),
    ],
)
def test_revision_chains_are_reconciled_or_held_explicitly(
    decisions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    expected: tuple[str, str | None],
    excluded: list[str],
) -> None:
    source = fx.pair(live=decisions, outcomes=outcomes)
    snap = fx.reduce(parse_settings(fx.settings()), source, fx.at(12))
    (example,) = snap["examples"]
    assert (example["state"], example["reason"]) == expected
    assert [x["reason"] for x in snap["exclusions"]] == excluded
    assert example["eligible"] is (expected[0] == "verified")


def test_reopen_and_revision_sequence_follows_the_shared_reducer_contract(tmp_path: Path) -> None:
    """K5: final v1, reopen v2, stale v1, final v3 -> verified/pending 1/0, 0/1, 0/1, 1/0."""
    w = fx.world(tmp_path)
    _committed(w)
    v1 = fx.cor("c-v1", "R1", fx.at(10, 5))
    v2 = fx.cor("c-v2", "R1", fx.at(10, 30), ver=2, status="reopened", value=None)
    v3 = fx.cor("c-v3", "R1", fx.at(10, 45), ver=3, value="PARTIAL")
    observed = []
    for outcomes, now in (
        ([v1], fx.at(10, 20)),
        ([v1, v2], fx.at(10, 35)),
        ([v1, v2, v1], fx.at(10, 40)),
        ([v1, v2, v1, v3], fx.at(11)),
    ):
        w.clock.set(now)
        source = fx.pair(live=[fx.dec("R1", fx.at(10))], outcomes=outcomes, captured=now)
        snap = fx.reduce(parse_settings(fx.settings()), source, now)
        live = snap["counters"]["live"]
        observed.append((live["verified"], live["pending"]))
    assert observed == [(1, 0), (0, 1), (0, 1), (1, 0)]


def test_a_newer_decision_revision_clears_verification_until_it_is_reverified(
    tmp_path: Path,
) -> None:
    settings = parse_settings(fx.settings())
    first = fx.pair(live=[fx.dec("V1", fx.at(10))], outcomes=[fx.cor("c-1", "V1", fx.at(10, 5))])
    revised = fx.pair(
        live=[fx.dec("V1", fx.at(10)), fx.dec("V1", fx.at(10, 30), rev=2, action="REFUND")],
        outcomes=[fx.cor("c-1", "V1", fx.at(10, 5))],
    )
    assert _by_ticket(fx.reduce(settings, first, fx.at(12)))["V1"]["state"] == "verified"
    example = _by_ticket(fx.reduce(settings, revised, fx.at(12)))["V1"]
    assert (example["state"], example["reason"]) == ("pending", "superseded_decision_revision")


def test_an_immature_correction_stays_pending_until_its_window_closes() -> None:
    settings = parse_settings(fx.settings())
    source = fx.pair(
        live=[fx.dec("M1", fx.at(10))],
        outcomes=[fx.cor("c-1", "M1", fx.at(10, 5))],
        captured=fx.at(10, 10),
    )
    early = fx.reduce(settings, source, fx.at(10, 10))
    late = fx.reduce(settings, source, fx.at(10, 15))
    assert _by_ticket(early)["M1"]["reason"] == "correction_immature"
    assert _by_ticket(late)["M1"]["state"] == "verified"


@pytest.mark.parametrize(
    ("captured", "reason"),
    [(fx.at(12, 1), "capture_after_as_of"), (fx.at(0, 0, day=-1), "source_stale")],
)
def test_capture_freshness_is_checked_against_the_worker_clock(captured: str, reason: str) -> None:
    source = fx.pair(live=[fx.dec("F1", fx.at(10))], captured=captured)
    with pytest.raises(SnapshotHold) as caught:
        fx.reduce(parse_settings(fx.settings()), source, fx.at(12))
    assert caught.value.reason == reason


# --------------------------------------------------------------------------- acceptance 3, 4


def test_a_policy_change_mid_read_rolls_back_to_the_previous_committed_snapshot(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("P1", fx.at(10))], outcomes=[fx.cor("c-1", "P1", fx.at(10, 5))])
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(source))
    assert first["state"] == "committed"
    folder = tmp_path / "snapshots" / fx.TENANT_A / fx.WF / "snapshots"
    before = sorted(p.read_bytes() for p in folder.iterdir())

    def moving(_settings: Any) -> Any:
        w.store.commit(fx.settings(revision=2), principal=fx.CUSTODIAN)
        return fx.pair(live=[fx.dec("P2", fx.at(11))])

    w.clock.set(fx.at(12, 30))
    rolled = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-2", moving)
    assert rolled["state"] == "rolled_back"
    assert rolled["reason"] == "policy_changed_during_read"
    assert rolled["snapshot_digest"] == first["snapshot_digest"]
    assert rolled["policy_revision"] == first["policy_revision"] == "generated-policy-1"
    assert sorted(p.read_bytes() for p in folder.iterdir()) == before
    # A fresh read under the now-stable revision 2 commits normally.
    after = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-3", fx.reader(source))
    assert after["state"] == "committed" and after["policy_revision"] == "generated-policy-2"


def test_a_policy_change_during_the_first_read_holds_with_nothing_committed(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)

    def moving(_settings: Any) -> Any:
        w.store.commit(fx.settings(revision=2), principal=fx.CUSTODIAN)
        return fx.pair()

    held = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", moving)
    assert (held["state"], held["reason"], held["snapshot_digest"]) == (
        "held",
        "no_committed_snapshot",
        None,
    )
    assert w.producer.snapshots(fx.TENANT_A, fx.WF) == []


def test_an_identical_trigger_returns_the_same_receipt(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("I1", fx.at(10))], outcomes=[fx.cor("c-1", "I1", fx.at(10, 5))])
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(source))
    w.clock.set(fx.at(13))  # the worker clock moved; the receipt must not
    again = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(source))
    other_key = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-2", fx.reader(source))
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert other_key == first
    assert len(w.producer.snapshots(fx.TENANT_A, fx.WF)) == 1
    # The same trigger reused for a different read conflicts instead of returning old evidence.
    changed = fx.pair(live=[fx.dec("I2", fx.at(10))], captured=fx.at(12, 30))
    conflict = w.producer.trigger(fx.TENANT_A, fx.WF, "trigger-1", fx.reader(changed))
    assert (conflict["state"], conflict["reason"]) == ("held", "trigger_conflict")


def test_a_tampered_snapshot_file_is_refused(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("X1", fx.at(10))])
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    path = tmp_path / "snapshots" / fx.TENANT_A / fx.WF / "snapshots" / "snap-00000001.json"
    # A canonical, well-formed edit: only the pin can tell it from the committed bytes.
    text = path.read_text()
    assert text.count('"awaiting_outcome"') == 2
    path.write_text(text.replace('"awaiting_outcome"', '"correction_immature"'))
    with pytest.raises(ProfileError, match="snapshot_tampered"):
        w.producer.snapshot(fx.TENANT_A, fx.WF, receipt["snapshot_digest"])
    # The rollback and replay paths read the same verified list, so they refuse too.
    with pytest.raises(ProfileError, match="snapshot_tampered"):
        w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))


# --------------------------------------------------------------------------- acceptance 5


def test_no_protected_feature_or_label_appears_in_the_receipt_or_counters(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(
        live=[
            fx.dec("S1", fx.at(10), action="SENTINEL-PRED-DENY"),
            fx.dec("S2", fx.at(10), customer=fx.HELD_OUT),
        ],
        outcomes=[
            fx.cor("c-1", "S1", fx.at(10, 5), value=fx.LABEL_SENTINEL),
            fx.cor("c-2", "S2", fx.at(10, 5), value=fx.LABEL_SENTINEL),
        ],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    assert tuple(receipt) == RECEIPT_FIELDS
    assert set(receipt["counters"]) <= set(COUNTERS)
    assert all(type(v) is int or v == "<k" for v in receipt["counters"].values())
    assert all(type(v) is int or v == "<k" for v in receipt["reason_codes"].values())
    assert set(receipt["reason_codes"]) <= REASONS
    text = json.dumps(receipt)
    forbidden = (
        fx.PROTECTED_SENTINEL,
        fx.LABEL_SENTINEL,
        "SENTINEL-PRED",
        "S1",
        "S2",
        fx.TENANT_A,
        fx.WF,
        "case-",
        "amt ",
        fx.HELD_OUT,
        fx.VERIFIER,
    )
    for value in forbidden:
        assert value not in text, value
    # The customer-local snapshot holds digests, not values, and never the protected column.
    local = json.dumps(_snapshot(w, receipt))
    assert fx.PROTECTED_SENTINEL not in local and fx.LABEL_SENTINEL not in local
    assert "SENTINEL-PRED" not in local
    for path in (tmp_path / "snapshots").rglob("*"):
        if path.is_file():
            assert fx.PROTECTED_SENTINEL.encode() not in path.read_bytes()


def test_the_receipt_discloses_only_the_counters_the_settings_allow(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w, receipt_counters=["eligible"])
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(fx.pair()))
    assert receipt["counters"] == {"eligible": 0}


# --------------------------------------------------------------------------- acceptance 8, 9


def test_evidence_origin_is_carried_and_starter_cases_count_zero_toward_recurrence(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    starter = [fx.dec(f"ST{n}", fx.at(9), action="DENY") for n in range(3)]
    starter_truth = [fx.cor(f"c-st{n}", f"ST{n}", fx.at(9, 5)) for n in range(3)]
    only_starter = fx.pair(starter=starter, outcomes=starter_truth)
    first = _snapshot(w, w.producer.trigger(fx.TENANT_A, fx.WF, "t1", fx.reader(only_starter)))
    assert first["counters"]["starter"]["verified"] == 3
    assert first["counters"]["live"] == {
        "cases": 0,
        "decisions": 0,
        "verified": 0,
        "pending": 0,
        "disputed": 0,
    }
    assert first["metrics"]["independent_incumbent_error_groups"] == 0
    assert first["metrics"]["wrong_automatic_decisions"] == {
        "state": "unavailable",
        "reason": "zero_denominator",
    }
    assert {(e["origin"], e["reason"], e["eligible"]) for e in first["examples"]} == {
        ("starter_generated", "starter_origin", False)
    }

    with_live = fx.pair(
        starter=starter,
        live=[fx.dec("L1", fx.at(10), action="DENY")],
        outcomes=[*starter_truth, fx.cor("c-l1", "L1", fx.at(10, 5))],
    )
    second = _snapshot(w, w.producer.trigger(fx.TENANT_A, fx.WF, "t2", fx.reader(with_live)))
    assert second["counters"]["live"]["verified"] == 1
    assert second["metrics"]["wrong_automatic_decisions"] == {
        "state": "available",
        "numerator": 1,
        "denominator": 1,
    }
    assert second["metrics"]["independent_incumbent_error_groups"] == 1
    assert second["counters"]["eligible"] == 1
    assert second["evidence"]["origins"] == ["observed_decision", "starter_generated"]


def test_a_zero_denominator_is_reported_as_unavailable(tmp_path: Path) -> None:
    settings = parse_settings(fx.settings())
    source = fx.pair(
        live=[fx.dec("Z1", fx.at(10), action="REVIEW")],
        outcomes=[fx.cor("c-1", "Z1", fx.at(10, 5))],
    )
    snap = fx.reduce(settings, source, fx.at(12))
    assert snap["counters"]["live"]["verified"] == 1
    assert snap["metrics"]["wrong_automatic_decisions"] == {
        "state": "unavailable",
        "reason": "zero_denominator",
    }
    assert snap["metrics"]["post_simple_repair_residual_groups"]["state"] == "unavailable"


def test_held_out_customers_and_related_cases_follow_the_existing_partitioner(
    tmp_path: Path,
) -> None:
    settings = parse_settings(fx.settings())
    source = fx.pair(
        live=[
            fx.dec("H1", fx.at(10), customer=fx.HELD_OUT),
            fx.dec("D1", fx.at(10), thread="thr-shared"),
            fx.dec("D2", fx.at(10), thread="thr-shared"),
            # Same family thread as a held-out case: the group crosses the split.
            fx.dec("X1", fx.at(10), thread="thr-cross"),
            fx.dec("X2", fx.at(10), thread="thr-cross", customer=fx.HELD_OUT),
        ],
        outcomes=[fx.cor(f"c-{t}", t, fx.at(10, 5)) for t in ("H1", "D1", "D2", "X1", "X2")],
    )
    examples = _by_ticket(fx.reduce(settings, source, fx.at(12)))
    assert (examples["H1"]["partition"], examples["H1"]["reason"]) == (
        "protected",
        "protected_partition",
    )
    assert examples["D1"]["group"] == examples["D2"]["group"]
    assert examples["D1"]["eligible"] and examples["D2"]["eligible"]
    assert {examples[t]["reason"] for t in ("X1", "X2")} == {"group_crosses_split"}
    assert not examples["X1"]["eligible"]


def test_training_use_must_be_granted_for_any_example_to_be_eligible() -> None:
    settings = parse_settings(fx.settings(uses=["inspect", "evaluate"]))
    source = fx.pair(live=[fx.dec("U1", fx.at(10))], outcomes=[fx.cor("c-1", "U1", fx.at(10, 5))])
    (example,) = fx.reduce(settings, source, fx.at(12))["examples"]
    assert (example["state"], example["eligible"], example["reason"]) == (
        "verified",
        False,
        "training_not_permitted",
    )


# --------------------------------------------------------------------------- settings batch


@pytest.mark.parametrize(
    ("principal", "reason"),
    [
        (Principal("generated-admin", "admin"), "unknown_principal_role"),
        (Principal("generated-owner", "workflow_owner"), "custodian_required"),
    ],
)
def test_only_the_custodian_role_asserted_by_the_connection_commits_and_unknown_roles_fail(
    tmp_path: Path, principal: Principal, reason: str
) -> None:
    w = fx.world(tmp_path)
    with pytest.raises(ProfileError, match=reason):
        w.store.commit(fx.settings(), principal=principal)
    assert w.store.current(fx.TENANT_A, fx.WF) is None


def _mutated(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    raw = fx.settings()
    cursor: Any = raw
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return raw


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("adjudicators",), [{"actor_id": fx.VERIFIER, "role": "owner"}], "adjudicator_role"),
        (("sources", 0, "origin"), "imported", "settings_origin"),
        (("sources", 0, "role"), "oracle", "settings_source_role"),
        (("mapping", "correction", "verified"), "is_verified", "mapping_correction"),
        (("mapping", "decision", "features", "age"), "customer_age", "feature_outside_allowlist"),
        (("settings_revision",), True, "settings_revision"),
        (("grant_epoch",), 3.0, "settings_grant_epoch"),
        (("uses",), ["inspect", "deploy"], "settings_uses"),
        (("receipt_counters",), ["eligible", "labels"], "settings_receipt_counters"),
        (("split",), {"mode": "random"}, "settings_split"),
    ],
)
def test_settings_refuse_unknown_roles_values_and_types(
    path: tuple[Any, ...], value: Any, reason: str
) -> None:
    with pytest.raises(ProfileError, match=reason):
        parse_settings(_mutated(path, value))


def test_settings_revisions_are_write_once_and_consecutive(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    assert w.store.commit(fx.settings(), principal=fx.CUSTODIAN).settings_revision == 1
    with pytest.raises(ProfileError, match="settings_conflict"):
        w.store.commit(fx.settings(maturity_seconds=0), principal=fx.CUSTODIAN)
    with pytest.raises(ProfileError, match="settings_revision_gap"):
        w.store.commit(fx.settings(revision=3), principal=fx.CUSTODIAN)
    current = w.store.current(fx.TENANT_A, fx.WF)
    assert current is not None and current.maturity_seconds == 600


# --------------------------------------------------------------------------- handoff artifacts


def test_the_committed_synthetic_source_pair_yields_the_committed_expected_counts() -> None:
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["fixture_kind"] == "generated"
    settings = parse_settings(fixture["settings"])
    source = fx.pair(
        live=fixture["source_pair"][fx.LIVE],
        starter=fixture["source_pair"][fx.STARTER],
        outcomes=fixture["source_pair"][fx.OUTCOMES],
        captured=fixture["captured_at"],
    )
    snap = fx.reduce(settings, source, fixture["as_of"])
    local = fixture["expected"]["snapshot"]
    assert snap["counters"] == local["counters"]
    assert snap["reason_codes"] == local["reason_codes"]
    assert snap["metrics"] == local["metrics"]
    # Two decisions on case-L1 count as one case everywhere.
    assert local["counters"]["live"]["decisions"] == local["counters"]["live"]["cases"] + 1
    # What leaves the worker: the protected case is a small cell, never a number.
    released = make_receipt({**snap, "settings_store": None})
    assert released["counters"] == fixture["expected"]["receipt"]["counters"]
    assert released["reason_codes"] == fixture["expected"]["receipt"]["reason_codes"]
    assert released["reason_codes"]["protected_partition"] == "<k"


def test_the_documented_receipt_and_reason_vocabulary_match_the_implementation() -> None:
    """R8: documentation drift is a defect. Extract the documented names and compare."""
    text = DOC.read_text()

    def section(title: str) -> set[str]:
        body = text.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]
        return {line.split("`")[1] for line in body.splitlines() if line.startswith("| `")}

    assert section("Receipt fields") == set(RECEIPT_FIELDS)
    assert section("Reason codes") == REASONS
    assert section("Receipt counters") == set(COUNTERS)
    body = text.split("## Reason codes\n", 1)[1].split("\n## ", 1)[0]
    documented = {
        line.split("`")[1]: line.rstrip(" |").rsplit("|", 1)[1].strip()
        for line in body.splitlines()
        if line.startswith("| `")
    }
    assert documented == {code: REASON_COUNTER.get(code, "none (hold)") for code in REASONS}


def test_the_drift_check_fails_on_real_drift(tmp_path: Path) -> None:
    edited = DOC.read_text().replace("| `trigger_conflict`", "| `trigger_clash`")
    body = edited.split("## Reason codes\n", 1)[1].split("\n## ", 1)[0]
    found = {line.split("`")[1] for line in body.splitlines() if line.startswith("| `")}
    assert found != REASONS


# --------------------------------------------------------------------------- review findings
# Each test below reproduces one review finding on 2779f27 and fails there.


class _Liar(str):
    """A ``str`` whose buffer says one thing while equality and hashing claim another."""

    claim = ""

    def __eq__(self, other: object) -> bool:
        return other == self.claim

    def __ne__(self, other: object) -> bool:
        return other != self.claim

    def __hash__(self) -> int:
        return hash(self.claim)


def _liar(buffer: str, claim: str) -> str:
    value = _Liar(buffer)
    value.claim = claim
    return value


def test_f1_a_self_asserted_custodian_is_refused_once_a_custodian_is_bound(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    # The first commit bound its actor on first use and every revision names its committer.
    assert w.store.custodians(fx.TENANT_A, fx.WF) == (1, frozenset({fx.CUSTODIAN.actor_id}))
    audit = w.store.committed_by(fx.TENANT_A, fx.WF, 1)
    assert audit["actor_id"] == fx.CUSTODIAN.actor_id and audit["binding_revision"] == 1
    assert audit["basis"] == "role_asserted_by_resolved_connection_bound_on_first_use"
    attacker = Principal("attacker-anyone", "data_custodian")
    forged = fx.settings(
        revision=2,
        adjudicators=[
            {"actor_id": fx.VERIFIER, "role": "verifier"},
            {"actor_id": "attacker-anyone", "role": "verifier"},
        ],
    )
    with pytest.raises(ProfileError, match="custodian_not_bound"):
        w.store.commit(forged, principal=attacker)
    with pytest.raises(ProfileError, match="custodian_not_bound"):
        w.store.bind_custodian(fx.TENANT_A, fx.WF, "attacker-anyone", principal=attacker)
    current = w.store.current(fx.TENANT_A, fx.WF)
    assert current is not None and current.settings_revision == 1
    assert "attacker-anyone" not in current.adjudicators


def test_f1_bindings_change_only_through_a_bound_custodian_and_the_last_one_stays(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    second = Principal("generated-custodian-2", "data_custodian")
    bound = w.store.bind_custodian(fx.TENANT_A, fx.WF, second.actor_id, principal=fx.CUSTODIAN)
    assert bound == {fx.CUSTODIAN.actor_id, second.actor_id}
    w.store.commit(fx.settings(revision=2), principal=second)
    assert w.store.committed_by(fx.TENANT_A, fx.WF, 2)["actor_id"] == second.actor_id
    w.store.unbind_custodian(fx.TENANT_A, fx.WF, fx.CUSTODIAN.actor_id, principal=second)
    with pytest.raises(ProfileError, match="custodian_not_bound"):
        w.store.commit(fx.settings(revision=3), principal=fx.CUSTODIAN)
    with pytest.raises(ProfileError, match="last_custodian"):
        w.store.unbind_custodian(fx.TENANT_A, fx.WF, second.actor_id, principal=second)
    # A binding file edited to add an actor is caught when the chain is re-verified.
    folder = tmp_path / "settings" / fx.TENANT_A / fx.WF / "custodians"
    path = folder / "bind-00000003.json"
    path.write_text(path.read_text().replace('"custodians":[', '"custodians":["x-1",'))
    with pytest.raises(ProfileError, match="custodian_binding_tampered"):
        w.store.commit(fx.settings(revision=3), principal=second)


def test_f1_a_store_with_revisions_but_no_binding_never_trusts_a_first_use(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    import shutil

    shutil.rmtree(tmp_path / "settings" / fx.TENANT_A / fx.WF / "custodians")
    with pytest.raises(ProfileError, match="custodian_binding_missing"):
        w.store.commit(fx.settings(revision=2), principal=Principal("x-2", "data_custodian"))


def _rogue(tmp_path: Path, w: fx.World) -> fx.World:
    """A copy of the settings store, sharing the original snapshots and dependencies."""
    import shutil

    from opentour_cell.pilot.candidate_snapshot import SnapshotProducer
    from opentour_cell.pilot.consumed_dependencies import DependencyRecorder
    from opentour_cell.pilot.learning_profile import SettingsBatchStore

    shutil.copytree(tmp_path / "settings", tmp_path / "rogue-settings")
    store = SettingsBatchStore(tmp_path / "rogue-settings")
    producer = SnapshotProducer(tmp_path / "snapshots", store, clock=w.clock)
    recorder = DependencyRecorder(tmp_path / "dependencies", producer, clock=w.clock)
    return fx.World(tmp_path, w.clock, store, producer, recorder)


def test_f2_a_copied_settings_store_cannot_commit_into_the_shared_snapshots(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(
        live=[fx.dec("R1", fx.at(10))],
        outcomes=[fx.cor("c-1", "R1", fx.at(10, 5), by="generated-rogue-verifier")],
    )
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "t1", fx.reader(source))
    assert _by_ticket(_snapshot(w, first))["R1"]["reason"] == "unattested_label"

    rogue = _rogue(tmp_path, w)
    rogue.store.commit(
        fx.settings(
            revision=2,
            adjudicators=[{"actor_id": "generated-rogue-verifier", "role": "verifier"}],
        ),
        principal=fx.CUSTODIAN,
    )
    w.clock.set(fx.at(12, 30))
    held = rogue.producer.trigger(fx.TENANT_A, fx.WF, "t2", fx.reader(source))
    assert (held["state"], held["reason"]) == ("held", "settings_store_mismatch")
    assert len(w.producer.snapshots(fx.TENANT_A, fx.WF)) == 1
    real = w.store.current(fx.TENANT_A, fx.WF)
    assert real is not None and real.settings_revision == 1
    # Reads through the rogue store are refused as well.
    for read in (
        lambda: rogue.producer.snapshot(fx.TENANT_A, fx.WF, first["snapshot_digest"]),
        lambda: rogue.producer.current(
            fx.TENANT_A, fx.WF, first["snapshot_digest"], fx.reader(source)
        ),
    ):
        with pytest.raises(ProfileError, match="settings_store_mismatch"):
            read()
    assert first["settings_store"] == w.store.identity(fx.TENANT_A, fx.WF)
    assert first["settings_store"] != rogue.store.identity(fx.TENANT_A, fx.WF)


def test_f2_a_planted_snapshot_or_a_removed_pin_is_refused(tmp_path: Path) -> None:
    from opentour_cell.pilot.learning_profile import _pin
    from opentour_cell.pilot.operating_point import canonical

    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("R1", fx.at(10))], outcomes=[fx.cor("c-1", "R1", fx.at(10, 5))])
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "t1", fx.reader(source))
    folder = tmp_path / "snapshots" / fx.TENANT_A / fx.WF
    # A canonical, pinned snapshot claiming a settings revision the store does not hold.
    planted = dict(_snapshot(w, first))
    planted["sequence"], planted["settings_revision"] = 2, 2
    body = canonical(planted)
    (folder / "snapshots" / "snap-00000002.json").write_text(body)
    (folder / "snapshots" / "snap-00000002.pin").write_text(_pin(body))
    with pytest.raises(ProfileError, match="settings_store_mismatch"):
        w.producer.snapshots(fx.TENANT_A, fx.WF)
    for suffix in ("json", "pin"):
        (folder / "snapshots" / f"snap-00000002.{suffix}").unlink()
    (folder / "settings-store.json").unlink()
    with pytest.raises(ProfileError, match="settings_store_mismatch"):
        w.producer.snapshot(fx.TENANT_A, fx.WF, first["snapshot_digest"])
    again = w.producer.trigger(fx.TENANT_A, fx.WF, "t2", fx.reader(source))
    assert (again["state"], again["reason"]) == ("held", "settings_store_mismatch")


def test_f4_the_receipt_filters_reason_codes_through_the_allowed_counters(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w, receipt_counters=["eligible"])
    source = fx.pair(
        live=[
            fx.dec("A1", fx.at(10)),
            fx.dec("P1", fx.at(10), customer=fx.HELD_OUT),
            fx.dec("D1", fx.at(10), state="disputed"),
        ],
        outcomes=[fx.cor("c-p1", "P1", fx.at(10, 5)), fx.cor("c-d1", "D1", fx.at(10, 5))],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    assert _snapshot(w, receipt)["reason_codes"] == {
        "awaiting_outcome": 1,
        "protected_partition": 1,
        "source_disputed": 1,
    }
    assert receipt["counters"] == {"eligible": 0}
    # Only codes that break down an allowed counter, and a single case is a small cell.
    assert receipt["reason_codes"] == {"protected_partition": "<k"}


def test_f4_cells_below_the_threshold_read_as_small_and_the_threshold_has_a_floor(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w, min_reportable_cell=3)
    tickets = [f"E{n}" for n in range(3)] + ["W1", "W2"]
    source = fx.pair(
        live=[fx.dec(t, fx.at(10)) for t in tickets],
        outcomes=[fx.cor(f"c-{t}", t, fx.at(10, 5)) for t in tickets[:3]],
    )
    receipt = w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source))
    assert receipt["counters"] == {"eligible": 3, "pending": "<k", "quarantined": 0, "disputed": 0}
    assert receipt["reason_codes"] == {"awaiting_outcome": "<k"}
    assert parse_settings(fx.settings()).min_reportable_cell == 5
    for bad in (1, 0, True, 2.0):
        with pytest.raises(ProfileError, match="settings_min_reportable_cell"):
            parse_settings(fx.settings(min_reportable_cell=bad))


def test_f6_live_totals_and_recurrence_count_distinct_cases_not_decisions() -> None:
    settings = parse_settings(fx.settings())
    source = fx.pair(
        live=[
            fx.dec("A1", fx.at(10), case="case-one", action="DENY"),
            fx.dec("A2", fx.at(10, 1), case="case-one", action="DENY"),
            fx.dec("B1", fx.at(10), case="case-two"),
            fx.dec("B2", fx.at(10, 1), case="case-two"),
        ],
        outcomes=[
            fx.cor("c-a1", "A1", fx.at(10, 5), case="case-one"),
            fx.cor("c-a2", "A2", fx.at(10, 6), case="case-one"),
            fx.cor("c-b1", "B1", fx.at(10, 5), case="case-two"),
        ],
    )
    snap = fx.reduce(settings, source, fx.at(12))
    assert snap["counters"]["live"] == {
        "cases": 2,
        "decisions": 4,
        "verified": 1,
        "pending": 1,  # case-two still has a decision without verified truth
        "disputed": 0,
    }
    assert snap["counters"]["eligible"] == 2
    assert snap["metrics"]["wrong_automatic_decisions"] == {
        "state": "available",
        "numerator": 2,
        "denominator": 2,
    }
    only_one = fx.pair(
        live=[
            fx.dec("A1", fx.at(10), case="case-one", action="DENY"),
            fx.dec("A2", fx.at(10, 1), case="case-one", action="DENY"),
        ],
        outcomes=[
            fx.cor("c-a1", "A1", fx.at(10, 5), case="case-one"),
            fx.cor("c-a2", "A2", fx.at(10, 6), case="case-one"),
        ],
    )
    snap = fx.reduce(settings, only_one, fx.at(12))
    assert (snap["counters"]["live"]["verified"], snap["counters"]["eligible"]) == (1, 1)
    assert snap["metrics"]["wrong_automatic_decisions"]["denominator"] == 1


def test_f7_label_digests_are_keyed_per_store_and_resist_vocabulary_enumeration(
    tmp_path: Path,
) -> None:
    import stat

    from opentour_cell.pilot.operating_point import digest

    source = fx.pair(live=[fx.dec("K1", fx.at(10))], outcomes=[fx.cor("c-1", "K1", fx.at(10, 5))])
    found = []
    for name in ("one", "two"):
        w = fx.world(tmp_path / name)
        _committed(w)
        snap = _snapshot(w, w.producer.trigger(fx.TENANT_A, fx.WF, "t", fx.reader(source)))
        (example,) = snap["examples"]
        found.append(example["correction"]["label_digest"])
        assert snap["evidence"]["label_digest_tier"] == "locally_keyed"
        vocabulary = ["REFUND", "DENY", "PARTIAL", "REVIEW", "ESCALATE"]
        unkeyed = {
            digest(["opentour.pilot.label.v1", fx.TENANT_A, example["example_ref"], v])
            for v in vocabulary
        }
        assert example["correction"]["label_digest"] not in unkeyed
        private = tmp_path / name / "snapshots" / fx.TENANT_A / fx.WF / "private"
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        assert stat.S_IMODE((private / "label.key").stat().st_mode) == 0o600
    assert found[0] != found[1]  # a different store holds a different key


def test_f7_a_missing_or_exposed_label_key_is_refused_never_re_keyed(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("K1", fx.at(10))], outcomes=[fx.cor("c-1", "K1", fx.at(10, 5))])
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "t1", fx.reader(source))
    private = tmp_path / "snapshots" / fx.TENANT_A / fx.WF / "private"
    private.chmod(0o750)
    with pytest.raises(ProfileError, match="private_key_folder_required"):
        w.producer.label_key(fx.TENANT_A, fx.WF)
    private.chmod(0o700)
    (private / "label.key").unlink()
    w.clock.set(fx.at(12, 30))
    held = w.producer.trigger(
        fx.TENANT_A, fx.WF, "t2", fx.reader(fx.pair(live=[fx.dec("K2", fx.at(10))]))
    )
    assert (held["state"], held["reason"]) == ("held", "label_key_missing")
    assert not (private / "label.key").exists()
    with pytest.raises(ProfileError, match="label_key_missing"):
        w.producer.current(fx.TENANT_A, fx.WF, first["snapshot_digest"], fx.reader(source))


def test_f8_an_unmapped_column_changes_neither_the_snapshot_digest_nor_the_receipt(
    tmp_path: Path,
) -> None:
    from opentour_cell.pilot.operating_point import digest

    settings = parse_settings(fx.settings())
    rows = [fx.dec("U1", fx.at(10))]
    changed = [fx.dec("U1", fx.at(10), customer_age="SENTINEL-PROTECTED-AGE-48")]
    outcomes = [fx.cor("c-1", "U1", fx.at(10, 5), unmapped_note="first")]
    outcomes_changed = [fx.cor("c-1", "U1", fx.at(10, 5), unmapped_note="second")]
    a = fx.reduce(settings, fx.pair(live=rows, outcomes=outcomes), fx.at(12))
    b = fx.reduce(settings, fx.pair(live=changed, outcomes=outcomes_changed), fx.at(12))
    assert digest(a) == digest(b)
    assert [s["capture_digest"] for s in a["sources"]] == [
        s["capture_digest"] for s in b["sources"]
    ]
    w = fx.world(tmp_path)
    _committed(w)
    first = w.producer.trigger(
        fx.TENANT_A, fx.WF, "t1", fx.reader(fx.pair(live=rows, outcomes=outcomes))
    )
    second = w.producer.trigger(
        fx.TENANT_A, fx.WF, "t2", fx.reader(fx.pair(live=changed, outcomes=outcomes_changed))
    )
    assert second == first
    # A mapped column still moves the digest.
    moved = fx.reduce(settings, fx.pair(live=[fx.dec("U1", fx.at(10), gateway="x")]), fx.at(12))
    assert digest(moved) != digest(a)


def test_f9_a_settings_commit_after_the_reduce_but_before_the_write_rolls_back(
    tmp_path: Path,
) -> None:
    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("P1", fx.at(10))], outcomes=[fx.cor("c-1", "P1", fx.at(10, 5))])
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "t1", fx.reader(source))

    def moving_clock() -> Any:
        # The worker clock is read inside the reduce: commit a new revision right then.
        if w.store.current(fx.TENANT_A, fx.WF).settings_revision == 1:  # type: ignore[union-attr]
            w.store.commit(fx.settings(revision=2), principal=fx.CUSTODIAN)
        return parse_time(fx.at(12, 30), "t")

    w.producer.clock = moving_clock
    later = fx.pair(live=[fx.dec("P2", fx.at(11))], captured=fx.at(12, 30))
    rolled = w.producer.trigger(fx.TENANT_A, fx.WF, "t2", fx.reader(later))
    assert (rolled["state"], rolled["reason"]) == ("rolled_back", "policy_changed_during_read")
    assert rolled["snapshot_digest"] == first["snapshot_digest"]
    assert len(w.producer.snapshots(fx.TENANT_A, fx.WF)) == 1


@pytest.mark.parametrize("side", ["decision", "correction"])
def test_f10_a_mapping_without_row_state_is_refused(tmp_path: Path, side: str) -> None:
    raw = fx.settings()
    del raw["mapping"][side]["row_state"]
    w = fx.world(tmp_path)
    with pytest.raises(ProfileError, match=f"mapping_{side}"):
        w.store.commit(raw, principal=fx.CUSTODIAN)
    assert w.store.current(fx.TENANT_A, fx.WF) is None


@pytest.mark.parametrize(
    "values",
    [
        {"admitted": ["admitted", "disputed"], "disputed": ["disputed"]},
        {"admitted": ["admitted"], "disputed": []},
        {"admitted": [], "disputed": ["disputed"]},
        {"admitted": ["admitted"]},
        {"admitted": ["admitted"], "disputed": ["bad value"]},
    ],
)
def test_f10_row_state_values_are_closed_disjoint_sets(values: dict[str, Any]) -> None:
    with pytest.raises(ProfileError, match="settings_row_state_values"):
        parse_settings(fx.settings(row_state_values=values))


def test_f10_a_declared_row_state_vocabulary_is_honoured_and_anything_else_quarantined() -> None:
    settings = parse_settings(
        fx.settings(row_state_values={"admitted": ["ok"], "disputed": ["contested"]})
    )
    source = fx.pair(
        live=[
            fx.dec("V1", fx.at(10), state="ok"),
            fx.dec("V2", fx.at(10), state="contested"),
            fx.dec("V3", fx.at(10), state="admitted"),
        ],
        outcomes=[fx.cor(f"c-{t}", t, fx.at(10, 5), state="ok") for t in ("V1", "V2", "V3")],
    )
    snap = fx.reduce(settings, source, fx.at(12))
    states = {k: (e["state"], e["eligible"]) for k, e in _by_ticket(snap).items()}
    assert states == {"V1": ("verified", True), "V2": ("disputed", False)}
    assert [q["reason"] for q in snap["quarantined"]] == ["unknown_row_state"]


def test_r24_a_decision_made_after_the_as_of_instant_is_not_in_the_snapshot() -> None:
    source = fx.pair(
        live=[fx.dec("N1", fx.at(10)), fx.dec("LATER", fx.at(12, 30))],
        outcomes=[fx.cor("c-1", "N1", fx.at(10, 5))],
    )
    snap = fx.reduce(parse_settings(fx.settings()), source, fx.at(12))
    assert set(_by_ticket(snap)) == {"N1"}
    assert snap["counters"]["live"]["decisions"] == 1


def test_r25_a_correction_made_after_the_as_of_instant_is_not_yet_known() -> None:
    source = fx.pair(live=[fx.dec("N1", fx.at(10))], outcomes=[fx.cor("c-1", "N1", fx.at(12, 30))])
    snap = fx.reduce(parse_settings(fx.settings()), source, fx.at(12))
    (example,) = snap["examples"]
    assert (example["state"], example["reason"]) == ("pending", "awaiting_outcome")


def test_r02_a_row_state_that_only_pretends_to_be_admitted_is_quarantined() -> None:
    source = fx.pair(
        live=[fx.dec("L1", fx.at(10), state=_liar("disputed", "admitted"))],
        outcomes=[fx.cor("c-1", "L1", fx.at(10, 5))],
    )
    snap = fx.reduce(parse_settings(fx.settings()), source, fx.at(12))
    assert snap["examples"] == []
    assert [q["reason"] for q in snap["quarantined"]] == ["unknown_row_state"]


def test_r13_a_role_that_only_pretends_to_be_the_custodian_is_refused(tmp_path: Path) -> None:
    w = fx.world(tmp_path)
    pretender = Principal("generated-owner", _liar("workflow_owner", "data_custodian"))
    with pytest.raises(ProfileError, match="unknown_principal_role"):
        w.store.commit(fx.settings(), principal=pretender)
    assert w.store.current(fx.TENANT_A, fx.WF) is None


def test_f1_a_pinned_binding_added_by_an_unbound_actor_is_refused(tmp_path: Path) -> None:
    from opentour_cell.pilot.learning_profile import write_pinned

    w = fx.world(tmp_path)
    _committed(w)
    # A well-formed, pinned binding revision whose author was never bound.
    write_pinned(
        tmp_path / "settings" / fx.TENANT_A / fx.WF / "custodians",
        "bind-00000002",
        {
            "schema": "opentour.pilot.custodian-binding.v1",
            "binding_revision": 2,
            "custodians": sorted([fx.CUSTODIAN.actor_id, "attacker-anyone"]),
            "change": {"kind": "bind", "actor_id": "attacker-anyone"},
            "by": "attacker-anyone",
            "basis": "role_asserted_by_resolved_connection_bound_on_first_use",
        },
        "x",
    )
    with pytest.raises(ProfileError, match="custodian_binding_tampered"):
        w.store.commit(
            fx.settings(revision=2), principal=Principal("attacker-anyone", "data_custodian")
        )


def test_f2_a_snapshot_naming_another_settings_store_is_refused(tmp_path: Path) -> None:
    from opentour_cell.pilot.learning_profile import _pin
    from opentour_cell.pilot.operating_point import canonical

    w = fx.world(tmp_path)
    _committed(w)
    source = fx.pair(live=[fx.dec("R1", fx.at(10))], outcomes=[fx.cor("c-1", "R1", fx.at(10, 5))])
    first = w.producer.trigger(fx.TENANT_A, fx.WF, "t1", fx.reader(source))
    planted = dict(_snapshot(w, first))
    planted["sequence"], planted["settings_store"] = 2, "0" * 64
    body = canonical(planted)
    folder = tmp_path / "snapshots" / fx.TENANT_A / fx.WF / "snapshots"
    (folder / "snap-00000002.json").write_text(body)
    (folder / "snap-00000002.pin").write_text(_pin(body))
    with pytest.raises(ProfileError, match="settings_store_mismatch"):
        w.producer.snapshots(fx.TENANT_A, fx.WF)
