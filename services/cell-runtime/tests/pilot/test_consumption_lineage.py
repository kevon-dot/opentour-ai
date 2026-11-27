"""OPE-544: claim -> consumed-field resolution and impact sets, on generated sentinel manifests.

Fixture kind: generated (local). Nothing here satisfies a provider, staged-packet (OPE-529)
or customer-evidence gate.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot.consumption_lineage import (
    OWNER,
    REVIEWER,
    Actor,
    Change,
    ConsumptionReader,
    Denial,
    GrantState,
    ImpactSet,
    LineageError,
    ManifestStore,
    Request,
    Resolution,
)

TENANT, OTHER = "generated-tenant-a", "generated-tenant-b"
WF = "generated-wf-refund"
NOW = "2026-01-01T12:00:00Z"
OWNER_ACTOR = Actor(TENANT, "owner-1", OWNER)
REVIEWER_ACTOR = Actor(TENANT, "reviewer-1", REVIEWER)


def field(
    source: str,
    path: str,
    revision: int = 1,
    at: str = "2026-01-01T09:58:00Z",
    span: str | None = "r1:c2",
) -> dict[str, Any]:
    return {
        "source_ref": source,
        "path": path,
        "source_revision": revision,
        "available_at": at,
        "span": span,
    }


def manifest(ref: str, kind: str = "simple_rule", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "opentour.pilot.consumed-field-manifest.v1",
        "manifest_ref": ref, "tenant": TENANT, "workflow_ref": WF, "engagement_epoch": 1,
        "grant_epoch": 3, "partition": "development", "rule_kind": kind,
        "case_ref": f"case-{ref}", "decision_time": "2026-01-01T10:00:00Z",
        "result_ref": f"result-{ref}", "candidate_ref": f"cand-{ref}",
        "example_ref": f"ex-{ref}", "correction_version": 2, "policy_revision": "pol-7",
        "claim_refs": [f"claim-{ref}"],
        "consumed": [field("refund-request", "amount"), field("payment-status", "state")],
        "unknown_dependencies": [],
        "descendants": [
            {"ref": f"ds-{ref}", "kind": "dataset", "derived_from": [f"ex-{ref}"]},
            {"ref": f"model-{ref}", "kind": "model", "derived_from": [f"ds-{ref}"]},
        ],
        "later_relationships": [
            {"kind": "settlement", "ref": "settlement-s1",
             "available_at": "2026-01-01T10:15:00Z", "relationship_version": 1},
        ],
    }  # fmt: skip
    base.update(over)
    return base


class Grants:
    def __init__(self) -> None:
        self.state: dict[tuple[str, str], GrantState] = {}
        self.set(TENANT)

    def set(self, tenant: str, **over: Any) -> None:
        values: dict[str, Any] = {
            "tenant": tenant,
            "workflow_ref": WF,
            "grant_epoch": 3,
            "revoked": False,
            "expires_at": "2026-02-01T00:00:00Z",
            "roles": {"owner-1": OWNER, "reviewer-1": REVIEWER},
        }
        values.update(over)
        self.state[(tenant, WF)] = GrantState(**values)

    def __call__(self, tenant: str, workflow: str) -> GrantState | None:
        return self.state.get((tenant, workflow))


def request(actor: Actor = OWNER_ACTOR, epoch: int = 3) -> Request:
    return Request(actor, WF, epoch, NOW)


def build(
    tmp_path: Path, *manifests: dict[str, Any]
) -> tuple[ManifestStore, Grants, ConsumptionReader]:
    store, grants = ManifestStore(tmp_path / "m"), Grants()
    for m in manifests:
        store.put(m)
    return store, grants, ConsumptionReader(store, grants)


def resolved(result: Resolution | Denial) -> Resolution:
    assert isinstance(result, Resolution), result
    return result


def impact(result: ImpactSet | Denial) -> ImpactSet:
    assert isinstance(result, ImpactSet), result
    return result


def oracle(manifests: list[dict[str, Any]], change: Change) -> tuple[set[str], set[str], set[str]]:
    """Independent brute-force dirty/unaffected/unknown computation (no shared code)."""
    dirty: set[str] = set()
    unknown: set[str] = set()
    universe: set[str] = set()
    for m in manifests:
        parents: dict[str, list[str]] = {
            m["result_ref"]: [],
            m["candidate_ref"]: [],
            m["example_ref"]: [],
        }
        parents.update({d["ref"]: d["derived_from"] for d in m["descendants"]})
        universe |= set(parents)
        hit = False
        for e in m["consumed"]:
            a, b = e["path"].split("/"), (change.path or "").split("/")
            same_field = change.path is None or a[: len(b)] == b[: len(a)]
            if e["source_ref"] == change.source_ref and same_field:
                hit = (
                    hit or change.new_revision is None or change.new_revision > e["source_revision"]
                )
        doubt = any(u in ("*", change.source_ref) for u in m["unknown_dependencies"])

        def below(seed: set[str], parents: dict[str, list[str]] = parents) -> set[str]:
            out = set(seed)
            while True:
                more = {r for r, p in parents.items() if set(p) & out} - out
                if not more:
                    return out
                out |= more

        seeds = {m["result_ref"], m["candidate_ref"], m["example_ref"]}
        if hit:
            dirty |= below(seeds)
        if doubt:
            unknown |= below(seeds)
        unknown |= {r for r, p in parents.items() if not p and r not in seeds}
    unknown -= dirty
    return dirty, universe - dirty - unknown, unknown


# ---------------------------------------------------------------- A1 refund timing


def test_refund_decision_at_1000_has_no_1015_settlement_among_inputs(tmp_path: Path) -> None:
    _, _, reader = build(tmp_path, manifest("r1"))
    r = resolved(reader.resolve_claim(request(), "claim-r1"))
    assert r.status == "resolved" and r.decision_time == "2026-01-01T10:00:00Z"
    assert all(
        i.source_ref != "settlement-s1" and i.available_at <= r.decision_time for i in r.inputs
    )
    assert r.later_relationships == ("settlement-s1",)  # attached by versioned relationship only
    assert {(i.source_ref, i.path, i.source_revision) for i in r.inputs} == {
        ("refund-request", "amount", 1),
        ("payment-status", "state", 1),
    }


def test_manifest_with_post_decision_input_is_rejected_not_repaired(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path)
    late = manifest("r1", consumed=[field("settlement-s1", "state", at="2026-01-01T10:15:00Z")])
    with pytest.raises(LineageError, match="input_after_decision"):
        store.put(late)
    assert store.all() == []


def test_resolution_returns_all_named_links(tmp_path: Path) -> None:
    _, _, reader = build(tmp_path, manifest("r1"))
    r = resolved(reader.resolve_claim(request(), "claim-r1"))
    assert (r.result_ref, r.candidate_ref, r.example_ref) == ("result-r1", "cand-r1", "ex-r1")
    assert (r.correction_version, r.policy_revision) == (2, "pol-7")
    assert {i.span for i in r.inputs} == {"r1:c2"}


# ---------------------------------------------------------------- A2/A3 impact


def test_changed_field_affects_only_actual_consumers_simple_rule_and_local_adapter(
    tmp_path: Path,
) -> None:
    ms = [
        manifest("a", "simple_rule"),
        manifest(
            "b",
            "local_adapter",
            consumed=[field("refund-request", "amount", 2), field("ledger", "total")],
        ),
        manifest("c", "simple_rule", consumed=[field("ledger", "total")]),
    ]
    _, _, reader = build(tmp_path, *ms)
    change = Change("refund-request", "amount", 2)
    got = impact(reader.impact(request(), change))
    exp_dirty, exp_clean, exp_unknown = oracle([validated(m) for m in ms], change)
    assert (
        {r for r, _ in got.dirty} == exp_dirty == {"result-a", "cand-a", "ex-a", "ds-a", "model-a"}
    )
    assert set(got.unaffected) == exp_clean and set(got.unknown) == exp_unknown == set()
    # b already consumed revision 2 (not superseded); c never consumed the field.
    assert not any(r.endswith(("-b", "-c")) for r, _ in got.dirty)


def validated(m: dict[str, Any]) -> dict[str, Any]:
    from opentour_cell.pilot.consumption_lineage import validate_manifest

    return validate_manifest(m)


def test_descendant_reachable_only_transitively_is_dirty_with_reason(tmp_path: Path) -> None:
    _, _, reader = build(tmp_path, manifest("a"))
    got = impact(reader.impact(request(), Change("payment-status", "state", 9)))
    reasons = dict(got.dirty)
    assert reasons["model-a"] == "descendant_of_changed_field"
    assert reasons["result-a"] == "consumed_revision_superseded"


def test_unrelated_field_does_not_appear_and_prefix_paths_overlap(tmp_path: Path) -> None:
    _, _, reader = build(tmp_path, manifest("a"))
    assert impact(reader.impact(request(), Change("unrelated-doc", "amount", 9))).dirty == ()
    assert impact(reader.impact(request(), Change("refund-request", "amount_note", 9))).dirty == ()
    assert (
        impact(reader.impact(request(), Change("refund-request", None, 9))).dirty != ()
    )  # whole object
    assert (
        impact(reader.impact(request(), Change("refund-request", "amount/currency", 9))).dirty != ()
    )


def test_field_consumed_by_multiple_claims_dirties_each_exactly_once(tmp_path: Path) -> None:
    ms = [manifest("a", claim_refs=["claim-a1", "claim-a2"]), manifest("b")]
    _, _, reader = build(tmp_path, *ms)
    one = resolved(reader.resolve_claim(request(), "claim-a1"))
    two = resolved(reader.resolve_claim(request(), "claim-a2"))
    assert one.manifest_digest == two.manifest_digest and one.inputs == two.inputs
    got = impact(reader.impact(request(), Change("refund-request", "amount", 5)))
    refs = [r for r, _ in got.dirty]
    assert len(refs) == len(set(refs)) == 10  # 5 per manifest, a and b both consumed it


def test_superseded_consumed_revision_dirty_but_stale_and_current_changes_are_not(
    tmp_path: Path,
) -> None:
    _, _, reader = build(tmp_path, manifest("a", consumed=[field("refund-request", "amount", 4)]))
    assert impact(reader.impact(request(), Change("refund-request", "amount", 5))).dirty
    assert impact(reader.impact(request(), Change("refund-request", "amount", 4))).dirty == ()
    assert (
        impact(reader.impact(request(), Change("refund-request", "amount", 3))).dirty == ()
    )  # stale replay
    withdrawn = impact(reader.impact(request(), Change("refund-request", "amount", None)))
    assert dict(withdrawn.dirty)["result-a"] == "source_withdrawn"


# ---------------------------------------------------------------- adverse: missing linkage


def test_missing_linkage_returns_unknown_never_a_guess(tmp_path: Path) -> None:
    _, _, reader = build(tmp_path, manifest("a"))
    r = resolved(reader.resolve_claim(request(), "claim-not-recorded"))
    assert (r.status, r.reason, r.inputs, r.manifest_ref) == (
        "unknown",
        "linkage_missing",
        (),
        None,
    )


def test_unknown_dependency_holds_claim_and_marks_impact_unknown(tmp_path: Path) -> None:
    ms = [manifest("a", unknown_dependencies=["ledger"]), manifest("b")]
    _, _, reader = build(tmp_path, *ms)
    held = resolved(reader.resolve_claim(request(), "claim-a"))
    assert (held.status, held.reason, held.unknown_dependencies) == (
        "hold",
        "unknown_dependency",
        ("ledger",),
    )
    got = impact(reader.impact(request(), Change("ledger", "total", 2)))
    assert set(got.unknown) == {"result-a", "cand-a", "ex-a", "ds-a", "model-a"} and got.dirty == ()
    assert "result-a" not in got.unaffected
    other = impact(reader.impact(request(), Change("elsewhere", "x", 2)))
    assert other.unknown == ()  # doubt is scoped to the named source
    star = [manifest("a", unknown_dependencies=["*"])]
    _, _, reader2 = build(tmp_path / "s", *star)
    assert impact(reader2.impact(request(), Change("elsewhere", "x", 2))).unknown


def test_descendant_without_recorded_parent_is_unknown_not_unaffected(tmp_path: Path) -> None:
    orphan = [{"ref": "ds-orphan", "kind": "dataset", "derived_from": []}]
    ms = [manifest("a", descendants=orphan)]
    _, _, reader = build(tmp_path, *ms)
    got = impact(reader.impact(request(), Change("unrelated", "x", 2)))
    assert got.unknown == ("ds-orphan",) and "ds-orphan" not in got.unaffected


def test_two_manifests_claiming_same_claim_hold(tmp_path: Path) -> None:
    _, _, reader = build(
        tmp_path, manifest("a", claim_refs=["claim-x"]), manifest("b", claim_refs=["claim-x"])
    )
    r = resolved(reader.resolve_claim(request(), "claim-x"))
    assert (r.status, r.reason) == ("hold", "ambiguous_linkage")


# ---------------------------------------------------------------- authority


def test_grant_revoked_between_click_and_lookup_returns_no_links(tmp_path: Path) -> None:
    _, grants, reader = build(tmp_path, manifest("a"))
    clicked = request()  # grant valid at click
    grants.set(TENANT, revoked=True)  # revoked before lookup
    for out in (
        reader.resolve_claim(clicked, "claim-a"),
        reader.impact(clicked, Change("refund-request", "amount", 9)),
    ):
        assert isinstance(out, Denial) and out.reason == "grant_revoked"
        assert set(out.receipt) == {"schema", "status", "reason", "request_digest"}
        flat = json.dumps(out.receipt)
        assert "claim-a" not in flat and "result-a" not in flat and "refund-request" not in flat


def test_stale_epoch_and_expired_grant_are_denied(tmp_path: Path) -> None:
    _, grants, reader = build(tmp_path, manifest("a"))
    grants.set(TENANT, grant_epoch=4)
    denied = reader.resolve_claim(request(epoch=3), "claim-a")
    assert isinstance(denied, Denial) and denied.reason == "grant_stale"
    assert resolved(reader.resolve_claim(request(epoch=4), "claim-a")).status == "resolved"
    grants.set(TENANT, expires_at="2026-01-01T11:00:00Z")
    expired = reader.resolve_claim(request(epoch=3), "claim-a")
    assert isinstance(expired, Denial) and expired.reason == "grant_expired"


def test_foreign_tenant_gets_no_links_and_indistinguishable_from_unknown(tmp_path: Path) -> None:
    store, grants, reader = build(tmp_path, manifest("a"))
    grants.set(OTHER)
    foreign = Actor(OTHER, "owner-1", OWNER)
    out = reader.resolve_claim(Request(foreign, WF, 3, NOW), "claim-a")
    assert isinstance(out, Denial) or (out.status == "unknown" and out.inputs == ())
    got = reader.impact(Request(foreign, WF, 3, NOW), Change("refund-request", "amount", 9))
    assert (
        isinstance(got, ImpactSet)
        and got.dirty == ()
        and got.unaffected == ()
        and got.unknown == ()
    )
    # Same ids under the foreign tenant stay tenant-local.
    store.put(
        manifest(
            "a",
            tenant=OTHER,
            manifest_ref="a-other",
            claim_refs=["claim-a"],
            result_ref="result-other",
        )
    )
    mine = resolved(reader.resolve_claim(request(), "claim-a"))
    assert mine.result_ref == "result-a"
    theirs = resolved(reader.resolve_claim(Request(foreign, WF, 3, NOW), "claim-a"))
    assert theirs.result_ref == "result-other"


def test_no_grant_wrong_actor_role_and_unlisted_actor_are_unavailable(tmp_path: Path) -> None:
    _, grants, reader = build(tmp_path, manifest("a"))
    for actor in (
        Actor(TENANT, "stranger", OWNER),
        Actor(TENANT, "owner-1", REVIEWER),
        Actor(TENANT, "owner-1", "admin"),
    ):
        out = reader.resolve_claim(Request(actor, WF, 3, NOW), "claim-a")
        assert isinstance(out, Denial) and out.reason == "unavailable"
    nogrant = reader.resolve_claim(Request(OWNER_ACTOR, "wf-other", 3, NOW), "claim-a")
    assert isinstance(nogrant, Denial) and nogrant.reason == "unavailable"
    assert grants  # keep fixture used


def test_two_reader_roles_see_different_detail(tmp_path: Path) -> None:
    _, _, reader = build(tmp_path, manifest("a"), manifest("p", partition="protected"))
    owner = resolved(reader.resolve_claim(request(OWNER_ACTOR), "claim-a"))
    reviewer = resolved(reader.resolve_claim(request(REVIEWER_ACTOR), "claim-a"))
    assert all(i.span for i in owner.inputs) and all(i.span is None for i in reviewer.inputs)
    assert owner.later_relationships and not reviewer.later_relationships
    assert reviewer.inputs and [i.path for i in owner.inputs] == [i.path for i in reviewer.inputs]
    for actor in (OWNER_ACTOR, REVIEWER_ACTOR):
        prot = resolved(reader.resolve_claim(request(actor), "claim-p"))
        assert prot.status == "protected" and prot.inputs == () and prot.input_count == 2
        got = impact(reader.impact(request(actor), Change("refund-request", "amount", 9)))
        assert got.protected_counts == {"dirty": 5, "unknown": 0, "unaffected": 0}
        assert not any("-p" in r for r, _ in got.dirty) and not any(
            r.endswith("-p") for r in got.unaffected
        )


# ---------------------------------------------------------------- restart / retry / immutability


def test_replay_after_restart_gives_same_semantic_impact_and_digests(tmp_path: Path) -> None:
    ms = [manifest("a"), manifest("b", "local_adapter", consumed=[field("ledger", "total")])]
    store, grants, reader = build(tmp_path, *ms)
    change = Change("refund-request", "amount", 7)
    before = (
        impact(reader.impact(request(), change)),
        resolved(reader.resolve_claim(request(), "claim-a")),
    )
    again = ManifestStore(tmp_path / "m")  # fresh process view of the same directory
    reader2 = ConsumptionReader(again, grants)
    after = (
        impact(reader2.impact(request(), change)),
        resolved(reader2.resolve_claim(request(), "claim-a")),
    )
    assert before[0].digest == after[0].digest and before[0].to_json() == after[0].to_json()
    assert before[1].to_json() == after[1].to_json()
    assert [store.put(m) for m in ms] == [again.put(m) for m in ms]  # idempotent retry


def test_manifest_is_immutable_and_conflicting_rewrite_is_refused(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path)
    original = manifest("a")
    digest_one = store.put(original)
    assert store.put(copy.deepcopy(original)) == digest_one
    changed = manifest("a", policy_revision="pol-8")
    with pytest.raises(LineageError, match="manifest_conflict"):
        store.put(changed)
    assert store.all()[0]["policy_revision"] == "pol-7"


def test_tampered_manifest_file_is_detected_on_read(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path)
    store.put(manifest("a"))
    path = next(tmp_path.glob("manifest-a.json"))
    path.rename(tmp_path / "manifest-zzz.json")
    with pytest.raises(LineageError, match="manifest_tampered"):
        store.all()


@pytest.mark.parametrize(
    "bad,reason",
    [
        ({"schema": "x"}, "manifest_schema"),
        ({"extra": 1}, "manifest_unknown_key"),
        ({"partition": "other"}, "manifest_partition"),
        ({"claim_refs": []}, "manifest_claim_refs"),
        ({"consumed": [field("a", "bad path")]}, "manifest_consumed_path"),
        ({"consumed": [field("a", "p"), field("a", "p")]}, "manifest_consumed_duplicate"),
        (
            {"descendants": [{"ref": "d", "kind": "dataset", "derived_from": ["ghost"]}]},
            "manifest_descendant_parent",
        ),
    ],
)
def test_malformed_manifests_are_rejected(tmp_path: Path, bad: dict[str, Any], reason: str) -> None:
    with pytest.raises(LineageError, match=reason):
        ManifestStore(tmp_path).put(manifest("a", **bad))


# ---------------------------------------------------------------- independent verification


def test_reader_matches_independent_oracle_across_changes(tmp_path: Path) -> None:
    ms = [
        manifest("a"),
        manifest(
            "b",
            "local_adapter",
            consumed=[field("ledger", "total", 3), field("refund-request", "amount/currency", 1)],
        ),
        manifest("c", unknown_dependencies=["ledger"]),
        manifest(
            "d",
            consumed=[field("ledger", "total")],
            descendants=[{"ref": "o", "kind": "proposal", "derived_from": []}],
        ),
    ]
    _, _, reader = build(tmp_path, *ms)
    norm = [validated(m) for m in ms]
    for change in (
        Change("refund-request", "amount", 2),
        Change("refund-request", None, 2),
        Change("ledger", "total", 4),
        Change("ledger", "total", 3),
        Change("ledger", "total", None),
        Change("zzz", "q", 1),
        Change("refund-request", "amount/currency", 0),
    ):
        got = impact(reader.impact(request(), change))
        dirty, clean, unknown = oracle(norm, change)
        assert ({r for r, _ in got.dirty}, set(got.unaffected), set(got.unknown)) == (
            dirty,
            clean,
            unknown,
        ), change
        assert not (dirty & clean or dirty & unknown or clean & unknown)


def test_staged_packet_lookup_is_not_claimed_here() -> None:
    # OPE-529 "repeats lookup against a staged packet when available": no staged packet exists
    # in this repository, so this ticket cannot show it. Recorded as an external gate, not PASS.
    from opentour_cell.pilot import consumption_lineage

    assert not hasattr(consumption_lineage, "staged_packet")
