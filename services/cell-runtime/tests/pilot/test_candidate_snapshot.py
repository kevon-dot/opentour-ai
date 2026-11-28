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
    REASONS,
    RECEIPT_FIELDS,
    SnapshotHold,
    reduce_pair,
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
    assert receipt["reason_codes"] == {
        "contradictory_corrections": 1,
        "duplicate_correction": 1,
        "late_correction_revision": 1,
        "unattested_label": 2,
    }
    assert receipt["counters"] == {"eligible": 1, "pending": 3, "quarantined": 0, "disputed": 1}


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
    assert snap["counters"]["live"] == {"decisions": 2, "verified": 0, "pending": 0, "disputed": 2}
    assert receipt["counters"]["disputed"] == 2 and receipt["counters"]["eligible"] == 0
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
    assert receipt["counters"]["quarantined"] == 1


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
    snap = reduce_pair(parse_settings(fx.settings()), source, parse_time(fx.at(12), "t"))
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
        snap = reduce_pair(parse_settings(fx.settings()), source, parse_time(now, "t"))
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
    now = parse_time(fx.at(12), "t")
    assert _by_ticket(reduce_pair(settings, first, now))["V1"]["state"] == "verified"
    example = _by_ticket(reduce_pair(settings, revised, now))["V1"]
    assert (example["state"], example["reason"]) == ("pending", "superseded_decision_revision")


def test_an_immature_correction_stays_pending_until_its_window_closes() -> None:
    settings = parse_settings(fx.settings())
    source = fx.pair(
        live=[fx.dec("M1", fx.at(10))],
        outcomes=[fx.cor("c-1", "M1", fx.at(10, 5))],
        captured=fx.at(10, 10),
    )
    early = reduce_pair(settings, source, parse_time(fx.at(10, 10), "t"))
    late = reduce_pair(settings, source, parse_time(fx.at(10, 15), "t"))
    assert _by_ticket(early)["M1"]["reason"] == "correction_immature"
    assert _by_ticket(late)["M1"]["state"] == "verified"


@pytest.mark.parametrize(
    ("captured", "reason"),
    [(fx.at(12, 1), "capture_after_as_of"), (fx.at(0, 0, day=-1), "source_stale")],
)
def test_capture_freshness_is_checked_against_the_worker_clock(captured: str, reason: str) -> None:
    source = fx.pair(live=[fx.dec("F1", fx.at(10))], captured=captured)
    with pytest.raises(SnapshotHold) as caught:
        reduce_pair(parse_settings(fx.settings()), source, parse_time(fx.at(12), "t"))
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
    assert all(type(v) is int for v in receipt["counters"].values())
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
    assert first["counters"]["live"] == {"decisions": 0, "verified": 0, "pending": 0, "disputed": 0}
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
    snap = reduce_pair(settings, source, parse_time(fx.at(12), "t"))
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
    examples = _by_ticket(reduce_pair(settings, source, parse_time(fx.at(12), "t")))
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
    (example,) = reduce_pair(settings, source, parse_time(fx.at(12), "t"))["examples"]
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
def test_only_a_bound_custodian_commits_settings_and_unknown_roles_fail_closed(
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
    snap = reduce_pair(settings, source, parse_time(fixture["as_of"], "t"))
    assert snap["counters"] == fixture["expected"]["counters"]
    assert snap["reason_codes"] == fixture["expected"]["reason_codes"]
    assert snap["metrics"] == fixture["expected"]["metrics"]


def test_the_documented_receipt_and_reason_vocabulary_match_the_implementation() -> None:
    """R8: documentation drift is a defect. Extract the documented names and compare."""
    text = DOC.read_text()

    def section(title: str) -> set[str]:
        body = text.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]
        return {line.split("`")[1] for line in body.splitlines() if line.startswith("| `")}

    assert section("Receipt fields") == set(RECEIPT_FIELDS)
    assert section("Reason codes") == REASONS
    assert section("Receipt counters") == set(COUNTERS)


def test_the_drift_check_fails_on_real_drift(tmp_path: Path) -> None:
    edited = DOC.read_text().replace("| `trigger_conflict`", "| `trigger_clash`")
    body = edited.split("## Reason codes\n", 1)[1].split("\n## ", 1)[0]
    found = {line.split("`")[1] for line in body.splitlines() if line.startswith("| `")}
    assert found != REASONS
