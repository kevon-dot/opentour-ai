"""OPE-544: claim -> consumed-field resolution and impact sets, on generated sentinel manifests.

Fixture kind: generated (local). Nothing here satisfies a provider, staged-packet (OPE-529)
or customer-evidence gate.
"""

import copy
import dataclasses
import hashlib
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import consumption_lineage as lineage
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
from opentour_cell.pilot.operating_point import digest

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


def at(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


# The production worker clock, captured before any test pins it.
REAL_WORKER_NOW: Callable[[], datetime] | None = getattr(lineage, "_worker_now", None)


@pytest.fixture(autouse=True)
def worker_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Test-only seam: pin the worker's own clock (module attribute, never a parameter)."""

    def pin(when: str) -> None:
        moment = at(when)
        monkeypatch.setattr(lineage, "_worker_now", lambda: moment, raising=False)

    pin(NOW)
    return pin


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
        # Grounded = a seed, or has parents and every parent is grounded (least fixpoint).
        grounded = set(seeds)
        while True:
            more = {r for r, p in parents.items() if r not in grounded and p and set(p) <= grounded}
            if not more:
                break
            grounded |= more
        unknown |= set(parents) - grounded
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
    assert store.all(TENANT) == []


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


def test_past_request_time_cannot_skip_expiry_judged_on_worker_clock(
    tmp_path: Path, worker_clock: Callable[[str], None]
) -> None:
    """MEDIUM: a caller back-dating ``request.now`` must not revive an expired grant."""
    _, grants, reader = build(tmp_path, manifest("a"))
    grants.set(TENANT, expires_at="2026-01-01T11:00:00Z")
    worker_clock("2026-01-01T11:30:00Z")  # worker says: expired half an hour ago
    for stale in ("2026-01-01T10:59:59Z", "2026-01-01T09:00:00Z", "2000-01-01T00:00:00Z"):
        back_dated = Request(OWNER_ACTOR, WF, 3, stale)
        for out in (
            reader.resolve_claim(back_dated, "claim-a"),
            reader.impact(back_dated, Change("refund-request", "amount", 9)),
        ):
            assert isinstance(out, Denial) and out.reason == "grant_expired", (stale, out)
            assert "claim-a" not in json.dumps(out.receipt)


def test_future_request_time_does_not_extend_a_grant(
    tmp_path: Path, worker_clock: Callable[[str], None]
) -> None:
    _, grants, reader = build(tmp_path, manifest("a"))
    grants.set(TENANT, expires_at="2026-01-01T11:00:00Z")
    worker_clock("2026-01-01T11:30:00Z")
    for ahead in ("2026-01-01T11:30:01Z", "2026-06-01T00:00:00Z", "2999-01-01T00:00:00Z"):
        out = reader.resolve_claim(Request(OWNER_ACTOR, WF, 3, ahead), "claim-a")
        assert isinstance(out, Denial) and out.reason == "grant_expired", (ahead, out)


def test_caller_time_can_only_narrow_the_window(
    tmp_path: Path, worker_clock: Callable[[str], None]
) -> None:
    _, grants, reader = build(tmp_path, manifest("a"))
    grants.set(TENANT, expires_at="2026-01-01T11:00:00Z")
    worker_clock("2026-01-01T10:00:00Z")  # worker: still valid for an hour
    inside = Request(OWNER_ACTOR, WF, 3, "2026-01-01T10:00:00Z")
    assert resolved(reader.resolve_claim(inside, "claim-a")).status == "resolved"
    # A caller time at or past expiry shortens the window: refused although the worker is early.
    for late in ("2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z"):
        out = reader.resolve_claim(Request(OWNER_ACTOR, WF, 3, late), "claim-a")
        assert isinstance(out, Denial) and out.reason == "grant_expired", late
    # Boundary on the worker clock itself: expiry is inclusive.
    worker_clock("2026-01-01T11:00:00Z")
    out = reader.resolve_claim(Request(OWNER_ACTOR, WF, 3, "2026-01-01T10:00:00Z"), "claim-a")
    assert isinstance(out, Denial) and out.reason == "grant_expired"


def test_production_entry_point_uses_the_real_worker_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pin: the module's own clock decides, whatever time the caller sends."""
    assert REAL_WORKER_NOW is not None, "worker clock seam missing"
    monkeypatch.setattr(lineage, "_worker_now", REAL_WORKER_NOW)
    before = datetime.now(UTC)
    observed = REAL_WORKER_NOW()
    assert observed.tzinfo is not None and before <= observed <= datetime.now(UTC)
    _, grants, reader = build(tmp_path, manifest("a"))
    # 2026-02-01 is in the past on any real clock running this suite; the caller claims January.
    out = reader.resolve_claim(request(), "claim-a")
    assert isinstance(out, Denial) and out.reason == "grant_expired"
    later = (datetime.now(UTC) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
    grants.set(TENANT, expires_at=later)
    assert resolved(reader.resolve_claim(request(), "claim-a")).status == "resolved"


def test_worker_clock_is_not_reachable_from_the_production_entry_point() -> None:
    # The seam is a private module attribute (checked as imported, before any pin) taking no
    # arguments: nothing a caller passes through the reader or a request can reach it.
    assert REAL_WORKER_NOW is not None and callable(REAL_WORKER_NOW)
    assert list(inspect.signature(REAL_WORKER_NOW).parameters) == []
    assert list(inspect.signature(ConsumptionReader).parameters) == ["store", "grants"]
    assert [f.name for f in dataclasses.fields(Request)] == [
        "actor",
        "workflow_ref",
        "grant_epoch",
        "now",
    ]
    assert not any("clock" in name for name in dir(ConsumptionReader))
    with pytest.raises(TypeError):
        ConsumptionReader(None, None, clock=lambda: datetime.now(UTC))  # type: ignore[arg-type,call-arg]


def test_receipts_state_the_pin_is_digest_self_consistent_only(tmp_path: Path) -> None:
    """LOW: a plain sha256 pin is tamper evidence only; every reader receipt says so."""
    _, _, reader = build(tmp_path, manifest("a"), manifest("p", partition="protected"))
    for claim in ("claim-a", "claim-p", "claim-none"):
        body = resolved(reader.resolve_claim(request(), claim)).to_json()
        assert body["integrity"] == "digest_self_consistent_only", claim
    got = impact(reader.impact(request(), Change("refund-request", "amount", 9))).to_json()
    assert got["integrity"] == "digest_self_consistent_only"
    for doc in (lineage.__doc__, ManifestStore.__doc__):
        assert doc and "digest_self_consistent_only" in doc


class _StrSubclassBadStr(str):
    """A real str whose ``__str__`` lies (returns a non-str)."""

    def __str__(self) -> str:
        return 5  # type: ignore[return-value]


class _StrImpostor:
    """Not a str at all, but ``isinstance(x, str)`` is True through ``__class__``."""

    __class__ = str  # type: ignore[assignment]


def test_non_str_times_are_invalid_time_not_type_errors(tmp_path: Path) -> None:
    """LOW-2: only an exact ``str`` is a time; anything else is ``invalid_time``, never raised."""
    _, grants, reader = build(tmp_path, manifest("a"))
    for bad in (_StrSubclassBadStr(NOW), _StrImpostor()):
        sent = Request(OWNER_ACTOR, WF, 3, bad)  # type: ignore[arg-type]
        for out in (
            reader.resolve_claim(sent, "claim-a"),
            reader.impact(sent, Change("refund-request", "amount", 9)),
        ):
            assert isinstance(out, Denial) and out.reason == "invalid_time", (type(bad), out)
    for bad in (_StrSubclassBadStr("2026-02-01T00:00:00Z"), _StrImpostor()):
        grants.set(TENANT, expires_at=bad)
        out = reader.resolve_claim(request(), "claim-a")
        assert isinstance(out, Denial) and out.reason == "invalid_time", type(bad)


def test_integrity_marker_is_inside_the_digested_receipt_body(tmp_path: Path) -> None:
    """NIT-2: editing the marker breaks the receipt's reader_digest."""
    _, _, reader = build(tmp_path, manifest("a"))
    for receipt in (
        resolved(reader.resolve_claim(request(), "claim-a")).to_json(),
        impact(reader.impact(request(), Change("refund-request", "amount", 9))).to_json(),
    ):
        body = {k: v for k, v in receipt.items() if k != "reader_digest"}
        assert body["integrity"] == "digest_self_consistent_only"
        assert digest(body) == receipt["reader_digest"]
        forged = {**body, "integrity": "authenticated"}
        assert digest(forged) != receipt["reader_digest"]


def test_wording_does_not_overstate_what_the_pin_enforces() -> None:
    """NIT-1: unsigned digest pins are not called immutable, sealed or verified."""
    source = inspect.getsource(lineage)
    for overstated in ("immutable", "sealed manifest", "verified manifest", "Every reader receipt"):
        assert overstated not in source, overstated
    assert lineage.__doc__ and "denial receipt" in lineage.__doc__


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
        assert prot.status == "protected" and prot.inputs == () and prot.input_count == 0
        assert prot.to_json()["manifest_ref"] is None and prot.result_ref is None
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
    assert store.all(TENANT)[0]["policy_revision"] == "pol-7"


def folder_of(tmp_path: Path, tenant: str = TENANT) -> Path:
    return tmp_path / "m" / tenant


def test_edited_manifest_with_stale_pin_is_detected_and_fails_closed(tmp_path: Path) -> None:
    """F3: an edit of the manifest alone is caught by the sidecar pin (tamper-evidence only)."""
    _, _, reader = build(tmp_path, manifest("a"))
    path = folder_of(tmp_path) / "manifest-a.json"
    doc = json.loads(path.read_text())
    doc["policy_revision"] = "pol-evil"
    doc["consumed"] = doc["consumed"][:1]
    path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")))
    with pytest.raises(LineageError, match="manifest_tampered"):
        ManifestStore(tmp_path / "m").all(TENANT)
    out = reader.resolve_claim(request(), "claim-a")
    assert isinstance(out, Denial) and out.reason == "manifest_tampered"
    assert "pol-evil" not in json.dumps(out.receipt)
    got = reader.impact(request(), Change("refund-request", "amount", 9))
    assert isinstance(got, Denial) and got.reason == "manifest_tampered"


def test_missing_pin_and_renamed_manifest_are_tampered(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path)
    store.put(manifest("a"))
    folder = tmp_path / TENANT
    (folder / "manifest-a.sha256").unlink()
    with pytest.raises(LineageError, match="manifest_tampered"):
        store.all(TENANT)
    store.put(manifest("a"))  # retry heals the missing pin identically
    (folder / "manifest-a.json").rename(folder / "manifest-zzz.json")
    (folder / "manifest-a.sha256").rename(folder / "manifest-zzz.sha256")
    with pytest.raises(LineageError, match="manifest_tampered"):
        store.all(TENANT)


def test_corrupt_file_in_one_tenant_does_not_break_another_tenant(tmp_path: Path) -> None:
    """F4: storage is tenant-scoped and read errors are typed, fail-closed LineageErrors."""
    store, grants, reader = build(tmp_path, manifest("a"))
    grants.set(OTHER)
    store.put(manifest("b", tenant=OTHER, claim_refs=["claim-b"]))
    (folder_of(tmp_path, OTHER) / "manifest-b.json").write_text("{not json")
    other = Request(Actor(OTHER, "owner-1", OWNER), WF, 3, NOW)
    out = reader.resolve_claim(other, "claim-b")
    assert isinstance(out, Denial) and out.reason == "manifest_tampered"  # pin no longer matches
    assert resolved(reader.resolve_claim(request(), "claim-a")).status == "resolved"
    pinned = folder_of(tmp_path, OTHER) / "manifest-b.sha256"
    pinned.write_text(hashlib.sha256(b"{not json").hexdigest())
    with pytest.raises(LineageError, match="manifest_unreadable"):
        store.all(OTHER)
    unreadable = reader.impact(other, Change("x", "y", 1))
    assert isinstance(unreadable, Denial) and unreadable.reason == "manifest_unreadable"
    assert resolved(reader.resolve_claim(request(), "claim-a")).status == "resolved"


def test_other_tenant_writing_same_manifest_ref_neither_collides_nor_probes(
    tmp_path: Path,
) -> None:
    store = ManifestStore(tmp_path)
    store.put(manifest("a"))
    store.put(manifest("a", tenant=OTHER, policy_revision="pol-other"))  # no manifest_conflict
    assert store.all(TENANT)[0]["policy_revision"] == "pol-7"
    assert store.all(OTHER)[0]["policy_revision"] == "pol-other"


def test_malformed_request_and_grant_times_are_denials_not_exceptions(tmp_path: Path) -> None:
    """F5"""
    _, grants, reader = build(tmp_path, manifest("a"))
    for bad in ("yesterday", "2026-01-01Z", "", "2026-01-01T12:00:00"):
        out = reader.resolve_claim(Request(OWNER_ACTOR, WF, 3, bad), "claim-a")
        assert isinstance(out, Denial) and out.reason == "invalid_time"
    grants.set(TENANT, expires_at="never")
    out2 = reader.impact(request(), Change("refund-request", "amount", 9))
    assert isinstance(out2, Denial) and out2.reason == "invalid_time"


def test_timestamps_must_be_full_and_later_relationships_must_be_later(tmp_path: Path) -> None:
    """F6: date-only times are refused; a 'later' relationship cannot predate the decision."""
    store = ManifestStore(tmp_path)
    with pytest.raises(LineageError, match="manifest_decision_time"):
        store.put(manifest("a", decision_time="2026-01-01Z"))
    early = {"kind": "settlement", "ref": "s", "available_at": "2026-01-01T09:00:00Z"}
    with pytest.raises(LineageError, match="manifest_later_relationship"):
        store.put(manifest("a", later_relationships=[{**early, "relationship_version": 1}]))
    assert store.all(TENANT) == []


def test_descendant_of_an_orphan_is_unknown_not_unaffected(tmp_path: Path) -> None:
    """F1: doubt must flow down from a node with no recorded parent."""
    nodes = [
        {"ref": "orph", "kind": "dataset", "derived_from": []},
        {"ref": "child", "kind": "model", "derived_from": ["orph"]},
        {"ref": "grand", "kind": "proposal", "derived_from": ["child"]},
        {"ref": "mixed", "kind": "comparator", "derived_from": ["ex-a", "orph"]},
    ]
    _, _, reader = build(tmp_path, manifest("a", descendants=nodes))
    got = impact(reader.impact(request(), Change("unrelated-doc", "x", 9)))
    assert set(got.unknown) == {"orph", "child", "grand", "mixed"}
    assert set(got.unaffected) == {"result-a", "cand-a", "ex-a"}
    assert got.dirty == ()


def test_unreachable_cycle_is_unknown_not_unaffected(tmp_path: Path) -> None:
    """F1: a disconnected derived_from cycle is accepted by the validator but never trusted."""
    cycle = [
        {"ref": "x", "kind": "dataset", "derived_from": ["y"]},
        {"ref": "y", "kind": "model", "derived_from": ["x"]},
        {"ref": "z", "kind": "proposal", "derived_from": ["y"]},
    ]
    _, _, reader = build(tmp_path, manifest("a", descendants=cycle))
    got = impact(reader.impact(request(), Change("unrelated-doc", "x", 9)))
    assert set(got.unknown) == {"x", "y", "z"} and "x" not in got.unaffected
    dirty = impact(reader.impact(request(), Change("refund-request", "amount", 9)))
    assert {"x", "y", "z"} <= set(dirty.unknown) and not {"x", "y", "z"} & {
        r for r, _ in dirty.dirty
    }


def test_protected_claim_exposes_aggregate_status_only_even_with_unknown_dependency(
    tmp_path: Path,
) -> None:
    """F2: protected confinement is checked before the hold branch and leaks nothing per case."""
    for extra in ({}, {"unknown_dependencies": ["ledger"]}):
        _, _, reader = build(
            tmp_path / str(len(extra)), manifest("p", partition="protected", **extra)
        )
        for actor in (OWNER_ACTOR, REVIEWER_ACTOR):
            r = resolved(reader.resolve_claim(request(actor), "claim-p"))
            flat = json.dumps(r.to_json())
            assert (r.status, r.reason) == ("protected", "aggregate_only")
            for secret in (
                "result-p",
                "cand-p",
                "ex-p",
                "manifest_p",
                "pol-7",
                "ledger",
                "2026-01-01T10:00",
                "refund-request",
                "amount",
            ):
                assert secret not in flat, secret
            assert r.manifest_ref is None and r.manifest_digest is None
            assert r.correction_version is None and r.policy_revision is None
            assert r.decision_time is None and r.input_count == 0
            assert r.unknown_dependencies == () and r.inputs == ()


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
