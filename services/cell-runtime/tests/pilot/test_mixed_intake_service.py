"""OPE-531 acceptance: scoped mixed-folder intake through local and generated-MCP adapters.

Fixture kind: generated. The MCP transport is a generated connector transport; none of this is
native integration coverage of an installed customer connector.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import mixed_intake_fixture as fx
import pytest
from opentour_cell.pilot.mixed_intake.adapters import (
    AdapterError,
    AdapterUnavailable,
    LocalDirectoryAdapter,
    McpResourceAdapter,
    ShortcutBlocked,
)
from opentour_cell.pilot.mixed_intake.base_paths import (
    freeze_single_policy_starter,
    structured_only_intake,
)
from opentour_cell.pilot.mixed_intake.model import Json, Limits
from opentour_cell.pilot.mixed_intake.service import Binding, IntakeError, MixedIntakeService
from opentour_cell.pilot.mixed_intake.store import IntakeStore

NATIVE_GRANT = {"generated-n08": ("markdown",)}
EXPECTED_STATES = {
    "eligible": 8,
    "needs_rendition": 1,
    "locked": 1,
    "unsupported": 1,
    "denied": 1,
}


class Sink:
    """Counts calls to the OPE-505 port without deduplicating, so duplicates are visible."""

    def __init__(self) -> None:
        self.drafts: list[tuple[str, Json]] = []
        self.jobs: list[tuple[str, str]] = []

    def create_draft(self, draft_key: str, payload: Json) -> None:
        self.drafts.append((draft_key, payload))

    def enqueue_job(self, job_key: str, draft_key: str) -> None:
        self.jobs.append((job_key, draft_key))


def binding(**over: object) -> Binding:
    base: dict[str, object] = {
        "tenant": fx.TENANT,
        "workflow_ref": "generated-workflow-1",
        "purpose": "policy",
        "selected_roots": (fx.ROOT,),
        "source_roles": fx.ROLES,
        "rendition_grants": NATIVE_GRANT,
        "authority_epochs": {"access": 1, "provider_access": 3},
    }
    base.update(over)
    return Binding(**base)  # type: ignore[arg-type]


def local(fixture: fx.MixedFixture) -> LocalDirectoryAdapter:
    return LocalDirectoryAdapter(fixture.write_manifest(), fixture.root)


def mcp(fixture: fx.MixedFixture, **kw: object):
    server = fx.GeneratedMcpServer(fixture, **kw)  # type: ignore[arg-type]
    return server, McpResourceAdapter(server, connection=fx.CONNECTION)


@pytest.fixture
def fixture(tmp_path: Path) -> fx.MixedFixture:
    return fx.MixedFixture(tmp_path / "f")


@pytest.fixture
def svc(tmp_path: Path) -> MixedIntakeService:
    return MixedIntakeService(IntakeStore(tmp_path / "intake.db"))


def full_run(svc: MixedIntakeService, adapter, b: Binding | None = None) -> str:
    sid = svc.open_session(b or binding(), adapter)
    assert svc.run(sid, adapter)["status"] == "sealed"
    return sid


def assert_exact_acceptance_counts(svc: MixedIntakeService, sid: str) -> None:
    counts = svc.counts(sid)
    assert counts["occurrences"] == 12
    assert counts["eligible_occurrences"] == 8
    assert counts["distinct_source_content_identities"] == 7
    assert counts["per_state"] == EXPECTED_STATES
    assert counts["duplicate_admissions"] == 0
    assert counts["unavailable_cells"] == 1  # the formula cell, unavailable and not evaluated


# ------------------------------------------------------------------ twelve-entry acceptance


def test_twelve_entry_local_manifest_expected_inventory(svc, fixture) -> None:
    adapter = local(fixture)
    sid = full_run(svc, adapter)
    assert_exact_acceptance_counts(svc, sid)
    items = {o.native_id: o for o in svc.store.latest(sid)}
    assert items["generated-n09"].extraction_state == "needs_rendition"
    assert items["generated-n10"].extraction_state == "locked"
    assert items["generated-n11"].extraction_state == "unsupported"
    assert items["generated-n12"].extraction_state == "denied"
    assert items["generated-n12"].reason == "shortcut_outside_grant"
    # only entries 1/2 share a content identity; both occurrences and locators stay distinct
    n1, n2 = items["generated-n01"], items["generated-n02"]
    assert n1.content_identity == n2.content_identity and n1.content_digest == n2.content_digest
    assert n1.occurrence_id != n2.occurrence_id and n1.family_id != n2.family_id
    assert [s["locator"] for s in n1.spans] == [s["locator"] for s in n2.spans]
    assert n1.path != n2.path
    identities = {o.content_identity for o in items.values() if o.extraction_state == "eligible"}
    assert len(identities) == 7
    # copies never become two independent families
    assert svc.counts(sid)["independent_families_by_lane"]["policy"] == 4
    assert svc.counts(sid)["parse_reuse_hits"] == 1  # the copy reused permitted parse work


def test_parse_cache_is_never_reused_across_a_reader_upgrade(
    tmp_path: Path, fixture, monkeypatch
) -> None:
    """Review F6: cached parse work is keyed by the reader revisions that produced it."""
    from opentour_cell.pilot.mixed_intake import readers

    svc = MixedIntakeService(IntakeStore(tmp_path / "cache.db"))
    adapter = local(fixture)
    full_run(svc, adapter, binding(page_size=3))
    reused = full_run(svc, adapter, binding(page_size=4))
    assert svc.counts(reused)["parse_reuse_hits"] > 1  # control: a new session reuses the cache
    reads: list[str | None] = []
    original = readers.read_object

    def spy(data, name, *args, **kw):  # type: ignore[no-untyped-def]
        reads.append(name)
        return original(data, name, *args, **kw)

    monkeypatch.setattr(readers, "read_object", spy)
    monkeypatch.setitem(readers.READER_REVISIONS, "markdown", "opentour.reader.text@9.9.9")
    upgraded = full_run(svc, adapter, binding(page_size=5))
    # only the in-session copy reuses work; nothing parsed by the old reader is reused
    assert svc.counts(upgraded)["parse_reuse_hits"] == 1
    assert reads, "the upgraded session must parse again"
    policy = {o.native_id: o for o in svc.store.latest(upgraded)}["generated-n01"]
    assert policy.parser_revision == "opentour.reader.text@9.9.9"


def test_shortcut_has_zero_target_reads_and_symlink_is_guarded(svc, fixture) -> None:
    adapter = local(fixture)
    full_run(svc, adapter)
    c = adapter.counters
    assert "generated-n12" not in c.content_reads and c.shortcut_target_reads == 0
    assert fx.OUTSIDE_TARGET not in c.content_reads
    with pytest.raises(AdapterError):  # the node is a shortcut, never readable
        adapter.read("generated-n12", "generated-rev-12-1")
    with pytest.raises(ShortcutBlocked):  # the guarded door refuses the real symlink on disk
        adapter._open("restricted/generated-shortcut.lnk")
    assert c.blocked_shortcut_attempts == 1


def test_citation_from_each_successful_format_at_pinned_revision(svc, fixture) -> None:
    adapter = local(fixture)
    sid = full_run(svc, adapter)

    def cite(nid: str, rev: str, loc: Json) -> Json:
        return svc.inspect_citation(sid, nid, rev, loc)

    md = cite("generated-n01", "generated-rev-01-1", {"kind": "lines", "start": 3, "end": 4})
    assert md["text"].startswith("Refunds up to 500") and md["parser_revision"].startswith(
        "opentour"
    )
    pdf = cite(
        "generated-n03",
        "generated-rev-03-1",
        {"kind": "pdf", "page": 1, "passage": 2, "reading_order": 2, "lines": [1, 3]},
    )
    assert pdf["data"]["header"] == ["Region", "Limit", "Approver", "Code"]
    docx = cite(
        "generated-n04",
        "generated-rev-04-1",
        {"kind": "docx", "table": 1, "row": 2, "col": 1, "header": "Step"},
    )
    assert docx["text"] == "01"
    csv = cite(
        "generated-n05",
        "generated-rev-05-1",
        {"kind": "csv", "row": 1, "header": ["case_id", "decided_at", "decision", "amount"]},
    )
    assert csv["data"]["cells"]["case_id"] == "00417"
    jsonl = cite(
        "generated-n06",
        "generated-rev-06-1",
        {"kind": "jsonl", "row": 1, "paths": ["$.case_id", "$.outcome.label", "$.verified_at"]},
    )
    assert jsonl["data"]["values"]["$.outcome.label"] == "upheld"
    xlsx = cite(
        "generated-n07", "generated-rev-07-1", {"kind": "xlsx", "sheet": "Quotes", "cell": "A2"}
    )
    assert xlsx["text"] == "00417"
    native = cite("generated-n08", "generated-rev-08-1", {"kind": "lines", "start": 3, "end": 3})
    assert native["text"].startswith("Escalate disputes")
    # original and exported revision/digest stay distinct for the authorized rendition
    assert native["native_revision"] == "generated-rev-08-1"
    assert native["export_revision"] == "generated-export-rev-1"
    owner = svc.inventory(sid)["items"]
    row = next(r for r in owner if r["ref"] == "generated-n08")
    assert row["original_revision"] == "generated-native-rev-1"
    assert row["original_digest"] == "generated-native-digest-1"
    assert row["export_digest"] and row["export_digest"] != row["original_digest"]
    # stale pin and unavailable cells fail closed
    with pytest.raises(IntakeError) as stale:
        cite("generated-n01", "generated-rev-01-0", {"kind": "lines", "start": 3, "end": 4})
    assert stale.value.code == "REVISION_NOT_PINNED"
    with pytest.raises(IntakeError) as formula:
        cite(
            "generated-n07", "generated-rev-07-1", {"kind": "xlsx", "sheet": "Quotes", "cell": "D2"}
        )
    assert formula.value.code == "CITATION_UNAVAILABLE"
    with pytest.raises(IntakeError):
        cite("generated-n09", "generated-rev-09-1", {"kind": "pdf", "page": 1})


def test_roles_come_only_from_owner_declaration_never_path_or_label(svc, fixture) -> None:
    adapter = local(fixture)
    sid = full_run(svc, adapter, binding(source_roles={"generated-n01": "policy"}))
    roles = {o.native_id: o.source_role for o in svc.store.latest(sid)}
    # n05 is named "decisions" and n06 "outcomes"; with no declaration both stay unknown
    assert roles["generated-n05"] == "unknown" and roles["generated-n06"] == "unknown"
    assert roles["generated-n02"] == "unknown"  # a byte-identical copy inherits no role
    with pytest.raises(IntakeError):
        binding(source_roles={"generated-n05": "ground_truth"}).validate()


def test_preview_reads_no_content_and_trial_commits_nothing(svc, fixture) -> None:
    adapter = local(fixture)
    preview = svc.preview(binding(), adapter)
    assert preview["content_reads"] == 0 and adapter.counters.total_reads() == 0
    assert preview["estimated_objects"] == 11
    assert preview["capability_matrix"]["approved_rendition"] is True
    assert preview["type_support"]["supported_via_rendition"] == 1  # n08 rendition is granted
    rows = {r["native_id"]: r for r in preview["entries"]}
    assert rows["generated-n05"]["proposed_role"] == "observed_decision"
    assert rows["generated-n12"]["selection"] == "shortcut_not_followed"
    trial = svc.trial(binding(), adapter, 2)
    assert trial["committed"] is False and len(trial["results"]) == 2
    assert adapter.counters.total_reads() == 2
    with svc.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM parse_cache").fetchone()[0] == 0


def test_traversal_is_limited_to_selected_descendants_and_deselections(svc, fixture) -> None:
    adapter = local(fixture)
    b = binding(
        selected_roots=("generated-f-pol", "generated-f-res"),
        deselected=("generated-n09", "generated-n10", "generated-n11", "generated-n12"),
    )
    sid = full_run(svc, adapter, b)
    states = {o.native_id: o.extraction_state for o in svc.store.latest(sid)}
    assert states == {
        "generated-n01": "eligible",
        "generated-n03": "eligible",
        "generated-n09": "deselected",
        "generated-n10": "deselected",
        "generated-n11": "deselected",
        "generated-n12": "deselected",
    }
    for outside in ("generated-n02", "generated-n04", "generated-n05", "generated-n08"):
        assert outside not in adapter.counters.content_reads
    assert "generated-n09" not in adapter.counters.content_reads  # deselected = never read


def test_folder_cycle_is_an_edge_and_depth_limit_is_visible(svc, fixture) -> None:
    sid = full_run(svc, local(fixture))
    assert svc.counts(sid)["cycle_edges"] == 1
    assert all(o.format != "folder" for o in svc.store.latest(sid))
    adapter = local(fixture)
    shallow = binding(limits=Limits(max_folder_depth=0))
    sid2 = full_run(svc, adapter, shallow)
    states = svc.counts(sid2)["per_state"]
    assert states == {"over_depth": 5}
    assert svc.inventory(sid2)["limits"]["max_folder_depth"] == 0


def test_limits_are_enforced_per_object_and_visible(svc, fixture) -> None:
    tight = binding(limits=Limits(max_object_bytes=300))
    sid = full_run(svc, local(fixture), tight)
    per_state = svc.counts(sid)["per_state"]
    assert per_state.get("oversized", 0) >= 3
    rows = svc.inventory(sid)["items"]
    assert any(r["limit"] == "bytes" for r in rows)


# ------------------------------------------------------------------ MCP repeat


@pytest.mark.parametrize("opaque", [False, True])
def test_mcp_repeat_same_counts_and_no_restricted_name(tmp_path: Path, opaque: bool) -> None:
    fixture = fx.MixedFixture(tmp_path / "m", opaque_denied=opaque)
    server, adapter = mcp(fixture)
    svc = MixedIntakeService(IntakeStore(tmp_path / "m.db"))
    sid = full_run(svc, adapter)
    assert_exact_acceptance_counts(svc, sid)
    assert set(server.methods) <= {
        "opentour/capabilities",
        "opentour/changes",
        "resources/list",
        "resources/read",
    }
    assert server.methods.count("resources/list") == adapter.counters.list_calls > 5  # paginated
    assert server.target_reads == 0 and "generated-n12" not in server.reads
    assert adapter.counters.shortcut_target_reads == 0
    surfaces = json.dumps(
        [
            svc.inventory(sid, "owner"),
            svc.inventory(sid, "shared"),
            svc.counts(sid),
            svc.store.ledger(sid),
            server.log,
        ]
    )
    n12 = next(o for o in svc.store.latest(sid) if o.native_id == "generated-n12")
    assert n12.display_name is None and n12.extraction_state == "denied"
    if opaque:
        assert fx.RESTRICTED_NAME not in surfaces
        assert n12.reason == "provider_denied"
    shared = json.dumps(svc.inventory(sid, "shared"))
    assert "generated-shortcut" not in shared and "generated-n12" not in shared


def test_capability_matrix_is_declared_by_both_adapters_and_snapshot_limit_reported(
    tmp_path: Path,
) -> None:
    fixture = fx.MixedFixture(tmp_path / "c")
    local_info = local(fixture).describe()
    _, adapter = mcp(fixture)
    mcp_info = adapter.describe()
    assert local_info.capabilities.matrix() == mcp_info.capabilities.matrix()
    matrix = local_info.capabilities.matrix()
    for key in (
        "list",
        "read",
        "version",
        "pagination",
        "delta",
        "deletion",
        "permission",
        "approved_rendition",
    ):
        assert matrix[key] is True
    assert local_info.transport != mcp_info.transport
    _, snap = mcp(fixture, snapshot_only=True)
    caps = snap.describe().capabilities
    assert caps.snapshot_only and "snapshot_only:no_delta_feed" in caps.limits_reported()
    with pytest.raises(AdapterError):
        snap.changes(None)


def test_mcp_adapter_is_read_only(tmp_path: Path) -> None:
    _, adapter = mcp(fx.MixedFixture(tmp_path / "r"))
    with pytest.raises(AdapterError):
        adapter._call("resources/subscribe", {})
    with pytest.raises(AdapterError):
        adapter._call("tools/call", {})


# ------------------------------------------------------------------ isolation and binding


def test_tenant_isolation_and_binding_holds(tmp_path: Path, svc, fixture) -> None:
    adapter = local(fixture)
    with pytest.raises(IntakeError) as wrong:
        svc.open_session(binding(tenant="generated-tenant-b"), adapter)
    assert wrong.value.code == "TENANT_MISMATCH"
    with pytest.raises(IntakeError) as epoch:
        svc.open_session(binding(authority_epochs={"access": 1, "provider_access": 9}), adapter)
    assert epoch.value.code == "EPOCH_MISMATCH"
    with pytest.raises(IntakeError):
        svc.open_session(binding(purpose="anything"), adapter)
    # identical bytes under another tenant never merge identities
    other = fx.MixedFixture(tmp_path / "other")
    manifest = other.manifest() | {"tenant": "generated-tenant-b"}
    path = other.base / "b.json"
    path.write_text(json.dumps(manifest))
    adapter_b = LocalDirectoryAdapter(path, other.root)
    sid_a = full_run(svc, adapter)
    sid_b = full_run(svc, adapter_b, binding(tenant="generated-tenant-b"))
    ids_a = {o.content_identity for o in svc.store.latest(sid_a) if o.content_identity}
    ids_b = {o.content_identity for o in svc.store.latest(sid_b) if o.content_identity}
    assert ids_a and ids_b and ids_a.isdisjoint(ids_b)
    fam_a = {o.family_id for o in svc.store.latest(sid_a)}
    assert fam_a.isdisjoint({o.family_id for o in svc.store.latest(sid_b)})


# ------------------------------------------------------------------ restart / replay


def test_interrupt_after_a_page_and_replay_is_identical(tmp_path: Path, fixture) -> None:
    adapter = local(fixture)
    clean = MixedIntakeService(IntakeStore(tmp_path / "clean.db"))
    clean_sid = full_run(clean, adapter)
    # interrupted run: stop after one page, restart with a fresh service object, resume
    path = tmp_path / "resume.db"
    first = MixedIntakeService(IntakeStore(path))
    adapter2 = local(fixture)
    sid = first.open_session(binding(), adapter2)
    partial = first.run(sid, adapter2, max_pages=1)
    assert partial["status"] == "incomplete" and partial["fingerprint"] is None
    assert partial["complete"] is False
    second = MixedIntakeService(IntakeStore(path))
    assert second.run(sid, adapter2)["status"] == "sealed"
    assert sid == clean_sid
    assert second.semantic_manifest(sid) == clean.semantic_manifest(clean_sid)
    assert second.counts(sid)["per_state"] == clean.counts(clean_sid)["per_state"]
    assert second.counts(sid)["duplicate_admissions"] == 0
    assert second.inventory(sid)["fingerprint"] == clean.inventory(clean_sid)["fingerprint"]
    # delivering an already applied page again is a no-op
    replay = second.replay_page(sid, adapter2, "generated-root", None)
    assert replay == {"replayed": True, "admitted": 0}
    assert second.counts(sid)["page_replays_noop"] == 1
    assert second.semantic_manifest(sid) == clean.semantic_manifest(clean_sid)


def test_crash_after_page_admission_before_cursor_commit_adds_no_duplicates(
    tmp_path: Path, fixture
) -> None:
    class Crash(Exception):
        pass

    calls: list[str] = []

    def boom(point: str) -> None:
        calls.append(point)
        if point == "after_page_admission" and len(calls) == 3:
            raise Crash

    path = tmp_path / "crash.db"
    adapter = local(fixture)
    svc = MixedIntakeService(IntakeStore(path))
    sid = svc.open_session(binding(), adapter)
    with pytest.raises(Crash):
        svc.run(sid, adapter, fault=boom)
    resumed = MixedIntakeService(IntakeStore(path))
    assert resumed.run(sid, adapter)["status"] == "sealed"
    assert_exact_acceptance_counts(resumed, sid)
    # the page that was admitted before the crash was not read or admitted a second time
    # nothing admitted before the crash was read a second time
    assert set(adapter.counters.content_reads.values()) == {1}
    assert resumed.counts(sid)["revisits_skipped"] >= 1
    assert resumed.counts(sid)["duplicate_admission_attempts"] == 0


def test_replayed_page_and_crash_before_dispatch_yield_one_draft_and_one_job(
    tmp_path: Path, fixture
) -> None:
    sink = Sink()
    path = tmp_path / "d.db"
    svc = MixedIntakeService(IntakeStore(path), sink=sink)
    adapter = local(fixture)
    sid = full_run(svc, adapter)

    class Crash(Exception):
        pass

    def boom(point: str) -> None:
        if point == "after_intake_admission_before_dispatch":
            raise Crash

    with pytest.raises(Crash):
        svc.freeze_starter(sid, fault=boom)
    assert sink.drafts == [] and sink.jobs == []
    restarted = MixedIntakeService(IntakeStore(path), sink=sink)
    assert restarted.dispatch_pending() == 1
    assert len(sink.drafts) == 1 and len(sink.jobs) == 1
    again = restarted.freeze_starter(sid)  # replaying intake does not mint another draft
    assert again["created"] is False and again["dispatched"] == 0
    restarted.replay_page(sid, adapter, "generated-root", None)
    assert restarted.dispatch_pending() == 0
    assert len(sink.drafts) == 1 and len(sink.jobs) == 1


def test_dispatch_pending_records_a_hold_and_continues_and_freeze_sends_only_its_own(
    tmp_path: Path, fixture
) -> None:
    """Review F1 at the service port: one held draft never stops another draft's dispatch."""

    class Holding(Sink):
        def __init__(self, refuse: set[str]) -> None:
            super().__init__()
            self.refuse = refuse

        def create_draft(self, draft_key: str, payload: Json) -> None:
            if draft_key in self.refuse:
                raise IntakeError("GENERATED_HOLD")
            super().create_draft(draft_key, payload)

    store = IntakeStore(tmp_path / "held.db")
    adapter = local(fixture)
    plain = MixedIntakeService(store)
    held_sid = full_run(plain, adapter, binding(deselected=("generated-n07",)))
    held = plain.freeze_starter(held_sid)
    other_sid = full_run(plain, adapter, binding(deselected=("generated-n06",)))
    other = plain.freeze_starter(other_sid)
    sink = Holding({held["draft_key"]})
    svc = MixedIntakeService(store, sink=sink)
    own_sid = full_run(svc, adapter)
    own = svc.freeze_starter(own_sid)
    assert own["dispatched"] == 1  # only its own draft, never the other pending ones
    assert [k for k, _ in sink.drafts] == [own["draft_key"]]
    assert store.ledger(held_sid, "dispatch_held") == []
    assert svc.dispatch_pending() == 1  # the held draft is skipped, the next one still goes
    assert [k for k, _ in sink.drafts] == [own["draft_key"], other["draft_key"]]
    hold = {"kind": "dispatch_held", "draft_key": held["draft_key"], "reason": "GENERATED_HOLD"}
    assert store.ledger(held_sid, "dispatch_held") == [hold]
    assert svc.dispatch_pending() == 0
    assert store.ledger(held_sid, "dispatch_held") == [hold]  # recorded once per reason
    sink.refuse.clear()
    assert svc.dispatch_pending() == 1 and svc.dispatch_pending() == 0


# ------------------------------------------------------------------ changes over time


def test_rename_of_a_stable_object_mints_no_new_family(tmp_path: Path) -> None:
    fixture = fx.MixedFixture(tmp_path / "rn", delta=False)
    svc = MixedIntakeService(IntakeStore(tmp_path / "rn.db"), sink=Sink())
    adapter = local(fixture)
    sid = full_run(svc, adapter)
    before = svc.semantic_manifest(sid)
    first_draft = svc.freeze_starter(sid)
    fixture.rename("generated-n01", "generated-policy-renamed.md", "policies/generated-renamed.md")
    adapter2 = local(fixture)
    assert svc.run(sid, adapter2, incremental=True)["status"] == "sealed"
    assert svc.semantic_manifest(sid) == before
    row = next(r for r in svc.inventory(sid)["items"] if r["ref"] == "generated-n01")
    assert row["name"] == "generated-policy-renamed.md"
    assert adapter2.counters.content_reads == {}  # no re-read for a rename
    assert [e for e in svc.store.ledger(sid, "rename")] == [
        {"kind": "rename", "family": row["family_id"], "new_family": False}
    ]
    second_draft = svc.freeze_starter(sid)  # a rename alone is not fresh evidence
    assert second_draft["draft_key"] == first_draft["draft_key"]
    assert second_draft["created"] is False and second_draft["supersedes"] is None


def test_editing_one_policy_revision_invalidates_only_its_consumers(tmp_path: Path) -> None:
    fixture = fx.MixedFixture(tmp_path / "ed")
    sink = Sink()
    svc = MixedIntakeService(IntakeStore(tmp_path / "ed.db"), sink=sink)
    adapter = local(fixture)
    sid = full_run(svc, adapter)
    svc.register_consumer(sid, "map-for-n01", [("generated-n01", "generated-rev-01-1")])
    svc.register_consumer(sid, "benchmark-for-n05", [("generated-n05", "generated-rev-05-1")])
    svc.register_consumer(
        sid,
        "map-for-both",
        [("generated-n05", "generated-rev-05-1"), ("generated-n01", "generated-rev-01-1")],
    )
    first = svc.freeze_starter(sid)
    fixture.edit(
        "generated-n01",
        fx.MARKDOWN_POLICY + b"\nAdded rule 03 (generated).\n",
        "generated-rev-01-2",
    )
    adapter2 = local(fixture)
    assert svc.run(sid, adapter2, incremental=True)["mode"] == "delta"
    validity = svc.consumer_validity(sid)
    assert validity == {"map-for-n01": False, "benchmark-for-n05": True, "map-for-both": False}
    ledger = svc.store.ledger(sid, "invalidation")
    assert ledger[-1]["native_id"] == "generated-n01"
    assert set(ledger[-1]["consumers"]) == {"map-for-n01", "map-for-both"}
    assert adapter2.counters.content_reads == {"generated-n01": 1}  # unrelated objects untouched
    # the edit is fresh evidence: a successor linked to the prior OPE-505 draft, not a duplicate
    second = svc.freeze_starter(sid)
    assert second["created"] is True and second["supersedes"] == first["draft_key"]
    assert len(sink.drafts) == 2 and len(sink.jobs) == 2
    # the old revision stays citeable at its pin; the new one at its own
    assert svc.inspect_citation(
        sid, "generated-n01", "generated-rev-01-1", {"kind": "lines", "start": 1, "end": 1}
    )["text"].startswith("# Refund")
    assert svc.inspect_citation(
        sid, "generated-n01", "generated-rev-01-2", {"kind": "lines", "start": 8, "end": 8}
    )["text"].startswith("Added rule")


def test_withdrawn_grant_blocks_incremental_admission_without_disclosure(tmp_path: Path) -> None:
    fixture = fx.MixedFixture(tmp_path / "wd")
    svc = MixedIntakeService(IntakeStore(tmp_path / "wd.db"))
    adapter = local(fixture)
    sid = full_run(svc, adapter)
    svc.register_consumer(sid, "uses-streams", [("generated-n05", "generated-rev-05-1")])
    result = svc.withdraw_grant(sid, "generated-f-str")
    assert result["purged_occurrences"] == 3
    # changes land at the source under the withdrawn grant and elsewhere
    fixture.edit(
        "generated-n05",
        fixture.files["streams/generated-decisions.csv"]
        + b"00419,2026-01-14T00:00:00+00:00,approve,0100\n",
        "generated-rev-05-2",
    )
    fixture.add_leaf(
        "generated-n13",
        "generated-f-str",
        "generated-new.csv",
        "streams/generated-new.csv",
        b"a\n1\n",
    )
    fixture.edit(
        "generated-n01", fx.MARKDOWN_POLICY + b"\nAdded line (generated).\n", "generated-rev-01-2"
    )
    adapter2 = local(fixture)
    assert svc.run(sid, adapter2, incremental=True)["status"] == "sealed"
    reads = adapter2.counters.content_reads
    assert "generated-n05" not in reads and "generated-n13" not in reads
    assert reads == {"generated-n01": 1}  # unrelated eligible work continues
    latest = {o.native_id: o for o in svc.store.latest(sid)}
    assert "generated-n13" not in latest  # zero new admissions under the withdrawn grant
    assert svc.consumer_validity(sid)["uses-streams"] is False
    for nid in ("generated-n05", "generated-n06", "generated-n07"):
        o = latest[nid]
        assert o.extraction_state == "withdrawn" and not o.spans
        assert (o.display_name, o.path, o.content_digest, o.content_identity) == (None,) * 4
    assert latest["generated-n01"].native_revision == "generated-rev-01-2"
    disclosed = json.dumps([svc.inventory(sid, "owner"), svc.inventory(sid, "shared")])
    for secret in (
        "generated-decisions",
        "generated-outcomes",
        "generated-workbook",
        "generated-new.csv",
        "00417,2026-01-10",
    ):
        assert secret not in disclosed
    assert svc.counts(sid)["per_state"]["withdrawn"] == 3
    # a snapshot-style full re-scan after withdrawal also admits nothing under the grant
    adapter3 = local(fixture)
    svc.run(sid, adapter3, incremental=True)
    assert "generated-n13" not in {o.native_id for o in svc.store.latest(sid)}


def test_deletion_needs_tombstone_or_complete_authorized_reconciliation(tmp_path: Path) -> None:
    # explicit tombstone through the delta feed
    fixture = fx.MixedFixture(tmp_path / "t1")
    svc = MixedIntakeService(IntakeStore(tmp_path / "t1.db"))
    sid = full_run(svc, local(fixture))
    fixture.delete("generated-n06", tombstone=True)
    assert svc.run(sid, local(fixture), incremental=True)["mode"] == "delta"
    states = {o.native_id: o.extraction_state for o in svc.store.latest(sid)}
    assert states["generated-n06"] == "deleted"
    assert svc.store.latest(sid)[5].reason == "explicit_tombstone"
    # delta feed without a tombstone: disappearance is NOT declared
    f2 = fx.MixedFixture(tmp_path / "t2")
    svc2 = MixedIntakeService(IntakeStore(tmp_path / "t2.db"))
    sid2 = full_run(svc2, local(f2))
    f2.delete("generated-n06", tombstone=False)
    svc2.run(sid2, local(f2), incremental=True)
    assert {o.native_id: o.extraction_state for o in svc2.store.latest(sid2)}[
        "generated-n06"
    ] == "eligible"
    # snapshot-only source: a partial re-scan declares nothing, a complete one does
    f3 = fx.MixedFixture(tmp_path / "t3", delta=False)
    svc3 = MixedIntakeService(IntakeStore(tmp_path / "t3.db"))
    sid3 = full_run(svc3, local(f3))
    f3.delete("generated-n06", tombstone=False)
    partial = svc3.run(sid3, local(f3), incremental=True, max_pages=2)
    assert partial["status"] == "incomplete"
    assert {o.native_id: o.extraction_state for o in svc3.store.latest(sid3)}[
        "generated-n06"
    ] == "eligible"
    assert svc3.run(sid3, local(f3))["status"] == "sealed"
    final = {o.native_id: o for o in svc3.store.latest(sid3)}["generated-n06"]
    assert final.extraction_state == "deleted"
    assert final.reason == "absent_in_complete_authorized_reconciliation"


def test_sealed_fingerprint_binds_inputs_and_partial_scans_are_incomplete(
    tmp_path: Path, svc, fixture
) -> None:
    adapter = local(fixture)
    sid = svc.open_session(binding(), adapter)
    assert svc.run(sid, adapter, max_pages=2)["fingerprint"] is None
    assert svc.inventory(sid)["status"] == "incomplete"
    with pytest.raises(IntakeError) as held:
        svc.freeze_starter(sid)
    assert held.value.code == "SCAN_INCOMPLETE"
    sealed = svc.run(sid, adapter)
    assert len(sealed["fingerprint"]) == 64
    other = MixedIntakeService(IntakeStore(tmp_path / "other.db"))
    changed = binding(source_roles={**fx.ROLES, "generated-n07": "unknown"})
    sid2 = full_run(other, local(fixture), changed)
    assert other.inventory(sid2)["fingerprint"] != svc.inventory(sid)["fingerprint"]


# ------------------------------------------------------------------ starter scope


def test_one_usable_policy_with_deselected_attachment_freezes_scoped_starter(
    tmp_path: Path,
) -> None:
    fixture = fx.MixedFixture(tmp_path / "s")
    sink = Sink()
    svc = MixedIntakeService(IntakeStore(tmp_path / "s.db"), sink=sink)
    b = binding(
        selected_roots=("generated-f-arc", "generated-f-res"),
        deselected=("generated-n09", "generated-n10", "generated-n11", "generated-n12"),
        source_roles={"generated-n02": "policy"},
        required_strata=("observed_decision",),
    )
    sid = full_run(svc, local(fixture), b)
    result = svc.freeze_starter(sid)
    assert {e["state"] for e in result["exclusions"]} == {"deselected"}
    assert len(result["exclusions"]) == 4  # the exclusion stays visible
    # a required historical stratum is missing: the broader claim is held
    assert result["broader_claim_held"] is True
    assert result["missing_strata"] == ["observed_decision"]
    assert len(sink.drafts) == 1 and sink.drafts[0][1]["scope"] == "scoped_starter"
    assert sink.drafts[0][1]["hold_reason"].startswith("required_historical_stratum")


def test_starter_requires_a_usable_policy(tmp_path: Path) -> None:
    fixture = fx.MixedFixture(tmp_path / "np")
    svc = MixedIntakeService(IntakeStore(tmp_path / "np.db"))
    sid = full_run(svc, local(fixture), binding(selected_roots=("generated-f-res",)))
    with pytest.raises(IntakeError) as held:
        svc.freeze_starter(sid)
    assert held.value.code == "NO_USABLE_POLICY"


# ------------------------------------------------------------------ adapter unavailable


def test_base_starter_paths_run_with_the_adapter_unavailable(tmp_path: Path) -> None:
    def down():
        raise AdapterUnavailable

    sink = Sink()
    svc = MixedIntakeService(
        IntakeStore(tmp_path / "u.db"), connectors={fx.CONNECTION: down}, sink=sink
    )
    fixture = fx.MixedFixture(tmp_path / "u")
    sid = svc.open_session(binding(), local(fixture))
    route = svc.run_route(sid, fx.CONNECTION)
    assert route["status"] == "adapter_unavailable" and route["base_starter_blocked"] is False
    # one-policy-only path
    starter = freeze_single_policy_starter(
        fx.MARKDOWN_POLICY, tenant=fx.TENANT, workflow_ref="generated-workflow-1", sink=sink
    )
    assert starter["scope"] == "scoped_starter" and len(sink.drafts) == 1 and len(sink.jobs) == 1
    # structured-only path
    csv = structured_only_intake(
        fx.CSV_DECISIONS, name="d.csv", tenant=fx.TENANT, workflow_ref="generated-workflow-1"
    )
    assert csv["rows"] == 2 and csv["route"] == "structured_only"
    jsonl = structured_only_intake(
        fx.JSONL_OUTCOMES, name="o.jsonl", tenant=fx.TENANT, workflow_ref="generated-workflow-1"
    )
    assert jsonl["rows"] == 2 and jsonl["lineage"][0]["row"] == 1
    with pytest.raises(IntakeError):
        freeze_single_policy_starter(b"\xff\xfe", tenant="t", workflow_ref="w")
    assert svc.store.ledger(sid, "route_unavailable")


# ------------------------------------------------------------------ surfaces


def test_ui_and_mcp_surfaces_call_the_same_worker_service(svc, fixture) -> None:
    sid = full_run(svc, local(fixture))
    ui = svc.handle("intake.status", {"session_id": sid})
    mcp_view = svc.handle("intake.status", {"session_id": sid})
    assert ui == mcp_view and ui["view"] == "shared"
    text = json.dumps(ui)
    assert "generated-policy" not in text and "generated-n0" not in text
    assert ui["counts"]["per_state"] == EXPECTED_STATES
    with pytest.raises(IntakeError):
        svc.handle("intake.write_everything", {})


def test_invalid_cursor_and_unknown_session_fail_closed(svc, fixture) -> None:
    adapter = local(fixture)
    with pytest.raises(AdapterError):
        adapter.list_children("generated-root", "bm90LWEtY3Vyc29y", 3)
    with pytest.raises(IntakeError):
        svc.run("missing", adapter)


def test_rendition_and_ocr_authorization_paths(tmp_path: Path) -> None:
    fixture = fx.MixedFixture(tmp_path / "ocr")
    svc = MixedIntakeService(IntakeStore(tmp_path / "ocr.db"))
    # no rendition grant for the native document: needs rendition, never read
    adapter = local(fixture)
    sid = full_run(svc, adapter, binding(rendition_grants={}))
    states = {o.native_id: o.extraction_state for o in svc.store.latest(sid)}
    assert states["generated-n08"] == "needs_rendition"
    assert "generated-n08" not in adapter.counters.rendition_reads
    # OCR granted: still not eligible; explicit review is required
    adapter2 = local(fixture)
    granted = binding(rendition_grants={**NATIVE_GRANT, "generated-n09": ("ocr-text",)})
    sid2 = full_run(svc, adapter2, granted)
    n09 = {o.native_id: o for o in svc.store.latest(sid2)}["generated-n09"]
    assert n09.extraction_state == "needs_review"
    assert n09.reason == "ocr_rendition_requires_explicit_review"
    assert n09.export_digest and n09.original_digest is None
    assert svc.counts(sid2)["per_state"]["needs_review"] == 1
    assert replace(Limits(), max_rows=1).max_rows == 1


def test_child_acl_override_and_denied_parent_are_never_restored_by_a_parent_grant(
    tmp_path: Path,
) -> None:
    fixture = fx.MixedFixture(tmp_path / "acl")
    fixture.node("generated-n05")["access"] = "denied"  # child override inside a granted folder
    fixture.node("generated-f-res")["access"] = "denied"  # denied folder under a granted root
    fixture.node("generated-f-res")["opaque"] = True
    svc = MixedIntakeService(IntakeStore(tmp_path / "acl.db"))
    adapter = local(fixture)
    sid = full_run(svc, adapter)
    latest = {o.native_id: o for o in svc.store.latest(sid)}
    assert latest["generated-n05"].extraction_state == "denied"
    assert latest["generated-f-res"].extraction_state == "denied"
    for hidden in ("generated-n09", "generated-n10", "generated-n11", "generated-n12"):
        assert hidden not in latest  # a denied parent is never descended into
    reads = adapter.counters.content_reads
    assert "generated-n05" not in reads and not set(reads) & {"generated-n09", "generated-n10"}
    shared = json.dumps(svc.inventory(sid, "shared"))
    assert "generated-decisions" not in shared and "generated-restricted" not in shared


def test_run_time_limit_stops_the_run_as_incomplete_not_sealed(tmp_path: Path) -> None:
    fixture = fx.MixedFixture(tmp_path / "rt")
    ticks = iter(range(0, 10_000, 100))
    svc = MixedIntakeService(IntakeStore(tmp_path / "rt.db"), deadline_clock=lambda: next(ticks))
    adapter = local(fixture)
    sid = svc.open_session(binding(limits=Limits(max_run_seconds=150.0)), adapter)
    status = svc.run(sid, adapter)
    assert status["status"] == "incomplete" and status["fingerprint"] is None
    assert svc.inventory(sid)["limits"]["max_run_seconds"] == 150.0
