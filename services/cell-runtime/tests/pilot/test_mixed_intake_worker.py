"""OPE-531: the one worker entry point behind the UI route and the MCP tool, wired to OPE-505.

Fixture kind: generated sentinel. The `mcp-resource` connection runs over a generated connector
transport; none of this is native integration coverage of an installed customer connector, and
no provider or customer witness exists for anything asserted here.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import mixed_intake_fixture as fx
import pytest
from opentour_cell.pilot.intake_local import (
    STARTER_GRANT_VERSION,
    STARTER_PURPOSE,
    STARTER_SELECTION_VERSION,
)
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.mcp import PROTOCOL_VERSION, serve_tools
from opentour_cell.pilot.mixed_intake import worker as entry
from opentour_cell.pilot.mixed_intake.adapters import public_ref
from opentour_cell.pilot.mixed_intake.model import Json
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.training import LocalTrainingStore

OWNER = "generated-owner"
OBSERVER = "generated-observer"
LOCAL = fx.CONNECTION
REMOTE = "generated-connection-mcp"
WORKFLOW = "generated-workflow-1"
STARTER_KEY = "d" * 64
EXPECTED_STATES = {
    "eligible": 8,
    "needs_rendition": 1,
    "locked": 1,
    "unsupported": 1,
    "denied": 1,
}
ARTIFACT = Path(__file__).with_name("fixtures") / "mixed-intake-triage-comparison-v1.json"

# Independent gold dispositions for the twelve leaf entries, written as literals from the
# ticket's acceptance text, never computed by the readers or the worker.
EXPECTED_DISPOSITIONS = {
    1: ("generated-n01", "markdown", "policy", "eligible"),
    2: ("generated-n02", "markdown", "policy", "eligible"),
    3: ("generated-n03", "pdf", "policy", "eligible"),
    4: ("generated-n04", "docx", "policy", "eligible"),
    5: ("generated-n05", "csv", "observed_decision", "eligible"),
    6: ("generated-n06", "jsonl", "independent_final", "eligible"),
    7: ("generated-n07", "xlsx", "example", "eligible"),
    8: ("generated-n08", "native-markdown", "policy", "eligible"),
    9: ("generated-n09", "pdf", "unknown", "needs_rendition"),
    # An encrypted OOXML package is a CFB container (magic d0cf11e0), whatever its name says.
    10: ("generated-n10", "cfb", "unknown", "locked"),
    11: ("generated-n11", "binary", "unknown", "unsupported"),
    12: ("generated-n12", "shortcut", "unknown", "denied"),
}


def _private(path: Path, value: object) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _grant(**over: object) -> Json:
    grant: Json = {
        "kind": "local-directory",
        "purposes": ["policy", "historical_decision_traces"],
        "selected_roots": [fx.ROOT],
        "deselected": [],
        "source_roles": dict(fx.ROLES),
        "rendition_grants": {"generated-n08": ["markdown"]},
        "formula_attested": [],
        "authority_epochs": {"access": 1, "provider_access": 3},
        "required_strata": [],
        "page_size": 3,
    }
    grant.update(over)
    return grant


class World:
    """One private worker directory, one generated fixture, an optional OPE-505 starter grant."""

    def __init__(self, base: Path, *, opaque: bool = False):
        base.mkdir(mode=0o700)
        self.base = base
        self.fixture = fx.MixedFixture(base / "src", opaque_denied=opaque)
        self.manifest = self.fixture.write_manifest()
        self.directory = base / "worker"
        self.directory.mkdir(mode=0o700)
        self.servers: list[fx.GeneratedMcpServer] = []
        self.selection: Path | None = None
        self.record: Json = {
            "schema_version": entry.RECORD_VERSION,
            "tenant": fx.TENANT,
            "workflow_ref": WORKFLOW,
            "actors": {OWNER: "owner", OBSERVER: "observer"},
            "connections": {
                LOCAL: _grant(manifest=str(self.manifest), root=str(self.fixture.root)),
                REMOTE: _grant(kind="mcp-resource"),
            },
            "starter_selection": None,
        }
        self.save()

    def save(self) -> None:
        _private(self.directory / entry.RECORD_FILE, self.record)

    def server(self) -> fx.GeneratedMcpServer:
        server = fx.GeneratedMcpServer(self.fixture)
        self.servers.append(server)
        return server

    def worker(self, **kw: object) -> entry.MixedIntakeWorker:
        return entry.MixedIntakeWorker(
            self.directory,
            transports={REMOTE: self.server},
            **kw,  # type: ignore[arg-type]
        )

    def starter(
        self,
        *,
        tenant: str = fx.TENANT,
        policy: str = "generated-n01",
        revision: str = "generated-rev-01-1",
    ) -> Path:
        """The owner's sealed OPE-426 setup grant, consumed by the real OPE-505 intake."""
        directory = self.base / "starter"
        directory.mkdir(mode=0o700)
        training = LocalTrainingStore(directory / "training.sqlite3", tenant)
        LearningMandates(directory, tenant, training.engagement_id, owner="generated-founder")
        selection = {
            "schema_version": STARTER_SELECTION_VERSION,
            "tenant": tenant,
            "actor": "generated-intake-worker",
            "owner_id": "generated-founder",
            "engagement_epoch": training.engagement_id,
            "grant_ref": "generated-starter",
        }
        body = {
            **selection,
            "schema_version": STARTER_GRANT_VERSION,
            "custodian_id": "generated-custodian",
            "purpose": STARTER_PURPOSE,
            "policy_resource_ref": policy,
            "source_revision": revision,
            "setup_policy_revision": "generated-setup-rev-1",
            "creation_ref": "generated-creation-1",
            "expires_at": int(time.time()) + 600,
        }
        seal = hmac.digest(bytes.fromhex(STARTER_KEY), canonical(body).encode(), "sha256")
        path = directory / "selection.json"
        _private(path, selection)
        _private(directory / "starter-intake-key.json", {"key": STARTER_KEY})
        _private(
            directory / "starter-intake-grant-generated-starter.json",
            {"grant": body, "seal_sha256": seal.hex()},
        )
        self.selection = path
        self.record["starter_selection"] = str(path)
        self.save()
        return path

    def ope505_rows(self, table: str) -> int:
        assert self.selection is not None
        path = self.selection.parent / (
            "intake.sqlite3" if table.startswith("intake_") else "workflow-benchmarks.sqlite3"
        )
        if not path.exists():
            return 0
        with sqlite3.connect(path) as db:
            try:
                return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                return 0


def scanned(world: World, connection: str = LOCAL, **scope: object) -> tuple[object, str]:
    w = world.worker()
    opened = w.call(
        OWNER, "mixed_intake_open", {"connection_ref": connection, "purpose": "policy", **scope}
    )
    sid = opened["session_id"]
    assert w.call(OWNER, "mixed_intake_run", {"session_id": sid})["status"] == "sealed"
    return w, sid


def mcp_conversation(call, *calls: tuple[str, Json]) -> dict[int, Json]:
    frames: list[Json] = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "generated", "version": "1"},
            },
        },
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
    ]
    for index, (name, arguments) in enumerate(calls, start=3):
        frames.append(
            {"id": index, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
    source = io.BytesIO(
        "".join(json.dumps({"jsonrpc": "2.0", **f}) + "\n" for f in frames).encode()
    )
    sink = io.BytesIO()
    assert serve_tools(entry.SERVER_NAME, entry.TOOLS, call, source, sink) == 0
    return {item["id"]: item for item in map(json.loads, sink.getvalue().splitlines())}


def refused(w: entry.MixedIntakeWorker, actor: object, op: str, args: object) -> str:
    with pytest.raises(entry.WorkerRefused) as error:
        w.call(actor, op, args)
    return error.value.code


# ------------------------------------------------------------------ one entry, two surfaces


def test_ui_route_and_mcp_tool_call_the_same_entry_point(tmp_path: Path, monkeypatch) -> None:
    world = World(tmp_path / "w")
    w, sid = scanned(world)
    seen: list[tuple[object, str, object]] = []
    original = entry.MixedIntakeWorker.call

    def spy(self, actor, operation, arguments):  # type: ignore[no-untyped-def]
        seen.append((actor, operation, arguments))
        return original(self, actor, operation, arguments)

    monkeypatch.setattr(entry.MixedIntakeWorker, "call", spy)
    route = entry.ui_dispatch(w, OWNER)
    reads = [
        ("mixed_intake_status", {"session_id": sid}),
        ("mixed_intake_inventory", {"session_id": sid}),
        ("mixed_intake_triage_packet", {"session_id": sid}),
        (
            "mixed_intake_citation",
            {
                "session_id": sid,
                "native_id": "generated-n01",
                "revision": "generated-rev-01-1",
                "locator": {"kind": "lines", "start": 3, "end": 4},
            },
        ),
        ("mixed_intake_preview", {"connection_ref": LOCAL, "purpose": "policy"}),
    ]
    ui_results = []
    for name, args in reads:
        line = next(k for k, v in entry.UI_ROUTES.items() if v == name)
        status, body = route(line, args)
        assert status == 200, body
        ui_results.append(body)
    responses = mcp_conversation(entry.mcp_dispatch(w, OWNER), *reads)
    assert [t["name"] for t in responses[2]["result"]["tools"]] == list(entry.OPERATIONS)
    mcp_results = [responses[i]["result"]["structuredContent"] for i in range(3, 3 + len(reads))]
    assert ui_results == mcp_results  # identical bytes of meaning from the one entry
    assert [s[1] for s in seen] == [n for n, _ in reads] * 2
    assert all(actor == OWNER for actor, _, _ in seen)
    # every UI route maps to exactly one operation and every operation has one route
    assert sorted(entry.UI_ROUTES.values()) == sorted(entry.OPERATIONS)
    # refusals are encoded per surface, from the same code
    assert route("POST /v1/mixed-intake/write-everything", {}) == (404, {"error": "UNKNOWN_ROUTE"})
    bad = mcp_conversation(entry.mcp_dispatch(w, OWNER), ("mixed_intake_status", {}))
    assert bad[3]["result"] == {
        "isError": True,
        "content": [{"type": "text", "text": "INVALID_ARGUMENTS"}],
    }
    assert route(next(iter(entry.UI_ROUTES)), {}) == (400, {"error": "INVALID_ARGUMENTS"})


@pytest.mark.parametrize("opaque", [False, True])
def test_twelve_entry_acceptance_through_the_entry_for_local_and_generated_mcp(
    tmp_path: Path, opaque: bool
) -> None:
    world = World(tmp_path / "w", opaque=opaque)
    outputs: list[Json] = []
    counts = {}
    for connection in (LOCAL, REMOTE):
        w = world.worker()
        preview = w.call(
            OWNER, "mixed_intake_preview", {"connection_ref": connection, "purpose": "policy"}
        )
        assert preview["content_reads"] == 0
        opened = w.call(
            OWNER, "mixed_intake_open", {"connection_ref": connection, "purpose": "policy"}
        )
        run = w.call(OWNER, "mixed_intake_run", {"session_id": opened["session_id"]})
        status = w.call(OBSERVER, "mixed_intake_status", {"session_id": opened["session_id"]})
        owner = w.call(OWNER, "mixed_intake_inventory", {"session_id": opened["session_id"]})
        triage = w.call(OWNER, "mixed_intake_triage_packet", {"session_id": opened["session_id"]})
        outputs += [preview, opened, run, status, owner, triage]
        counts[connection] = status["counts"]
        assert status["counts"]["per_state"] == EXPECTED_STATES
        assert status["counts"]["eligible_occurrences"] == 8
        assert status["counts"]["distinct_source_content_identities"] == 7
        assert status["counts"]["duplicate_admissions"] == 0
        for result in (preview, opened, run, status, owner, triage):
            assert result["installed_connector_coverage"] is False
            assert result["evidence_tier"] == "simulated_unwitnessed"
        label = {LOCAL: "customer-local-directory", REMOTE: entry.GENERATED_TRANSPORT}[connection]
        assert preview["connector_transport"] == run["connector_transport"] == label
    # Parse work is reused per tenant and scoped root, so the second connection's hit count
    # differs by design; every inventory count must match exactly.
    for c in counts.values():
        c.pop("parse_reuse_hits")
    assert counts[LOCAL] == counts[REMOTE]
    server = world.servers[-1]
    assert server.target_reads == 0 and "generated-n12" not in server.reads
    if opaque:
        everything = json.dumps(outputs) + "".join(s for srv in world.servers for s in srv.log)
        assert fx.RESTRICTED_NAME not in everything
        with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
            dump = "\n".join(db.iterdump())
        assert fx.RESTRICTED_NAME not in dump
    # the shared view names nothing
    shared = json.dumps(outputs[3])
    assert "generated-policy" not in shared and "generated-n0" not in shared


# ------------------------------------------------------------------ authority


def test_grants_and_identity_come_only_from_the_worker_record(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    w = world.worker()
    scope = {"connection_ref": LOCAL, "purpose": "policy"}
    # caller-supplied authority is never accepted
    for extra in (
        {"tenant": "generated-tenant-b"},
        {"source_roles": {"generated-n07": "policy"}},
        {"rendition_grants": {"generated-n09": ["ocr-text"]}},
        {"formula_attested": ["generated-n07"]},
        {"authority_epochs": {"access": 9}},
        {"workflow_ref": "generated-other"},
    ):
        assert refused(w, OWNER, "mixed_intake_open", {**scope, **extra}) == "INVALID_ARGUMENTS"
    assert refused(w, OWNER, "mixed_intake_open", {**scope, "roots": ["generated-elsewhere"]}) == (
        "ROOT_NOT_GRANTED"
    )
    assert refused(
        w, OWNER, "mixed_intake_open", {**scope, "purpose": "independently_verified_outcomes"}
    ) == ("PURPOSE_NOT_GRANTED")
    assert refused(
        w, OWNER, "mixed_intake_open", {**scope, "connection_ref": "generated-unknown"}
    ) == ("CONNECTION_NOT_GRANTED")

    class Lookalike(str):
        pass

    # identity fields are exact `str`, never a subclass that might compare equal
    assert refused(w, Lookalike(OWNER), "mixed_intake_open", scope) == "ACTOR_NOT_AUTHORIZED"
    assert refused(
        w, OWNER, "mixed_intake_open", {**scope, "connection_ref": Lookalike(LOCAL)}
    ) == ("INVALID_ARGUMENTS")
    with pytest.raises(entry.WorkerRefused):
        entry.ui_dispatch(w, Lookalike(OWNER))
    with pytest.raises(entry.WorkerRefused):
        entry.mcp_dispatch(w, "generated-stranger")
    # roles come from the record: an observer reads only the shared status
    sid = w.call(OWNER, "mixed_intake_open", scope)["session_id"]
    assert (
        refused(w, OBSERVER, "mixed_intake_inventory", {"session_id": sid}) == "OWNER_ROLE_REQUIRED"
    )
    assert refused(w, OBSERVER, "mixed_intake_open", scope) == "OWNER_ROLE_REQUIRED"
    assert (
        refused(w, OWNER, "mixed_intake_run", {"session_id": Lookalike(sid)}) == "INVALID_ARGUMENTS"
    )
    assert (
        refused(w, OWNER, "mixed_intake_run", {"session_id": "generated-unknown"})
        == "UNKNOWN_SESSION"
    )
    # a caller may narrow by deselecting, never widen
    narrowed = w.call(OWNER, "mixed_intake_open", {**scope, "deselect": ["generated-n07"]})
    w.call(OWNER, "mixed_intake_run", {"session_id": narrowed["session_id"]})
    items = w.call(OWNER, "mixed_intake_inventory", {"session_id": narrowed["session_id"]})["items"]
    assert next(i for i in items if i["ref"] == "generated-n07")["state"] == "deselected"
    # the record is re-read on every call: a changed grant cannot resume, a removed actor stops
    world.record["connections"][LOCAL]["source_roles"]["generated-n07"] = "policy"
    world.save()
    assert refused(w, OWNER, "mixed_intake_run", {"session_id": sid}) == "GRANT_CHANGED"
    del world.record["actors"][OBSERVER]
    world.save()
    assert (
        refused(w, OBSERVER, "mixed_intake_status", {"session_id": sid}) == "ACTOR_NOT_AUTHORIZED"
    )
    # a record naming a connection whose manifest disagrees is refused before any scan
    world.record["connections"]["generated-connection-other"] = world.record["connections"][LOCAL]
    world.save()
    assert (
        refused(
            w, OWNER, "mixed_intake_open", {**scope, "connection_ref": "generated-connection-other"}
        )
        == "CONNECTION_MISMATCH"
    )


def test_a_record_that_is_not_private_or_not_exact_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    record_path = world.directory / entry.RECORD_FILE
    record_path.chmod(0o644)
    with pytest.raises(entry.WorkerRefused) as error:
        world.worker()
    assert error.value.code == "WORKER_RECORD_UNAVAILABLE"
    record_path.chmod(0o600)
    world.record["actors"][OWNER] = "superuser"
    world.save()
    with pytest.raises(entry.WorkerRefused) as error:
        world.worker()
    assert error.value.code == "INVALID_WORKER_RECORD"


def test_an_mcp_connection_without_a_transport_never_blocks_the_base_paths(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    w = entry.MixedIntakeWorker(world.directory)  # no installed connector transport
    result = w.call(OWNER, "mixed_intake_preview", {"connection_ref": REMOTE, "purpose": "policy"})
    assert result["status"] == "adapter_unavailable" and result["base_starter_blocked"] is False
    assert result["installed_connector_coverage"] is False


# ------------------------------------------------------------------ OPE-505 production wiring


def test_freeze_admits_one_ope505_benchmark_draft_through_the_production_intake(
    tmp_path: Path,
) -> None:
    world = World(tmp_path / "w")
    world.starter()

    class Crash(Exception):
        pass

    def boom(point: str) -> None:
        if point == "after_intake_admission_before_dispatch":
            raise Crash

    w, sid = scanned(world)
    crashing = world.worker(fault=boom)
    with pytest.raises(Crash):
        crashing.call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert world.ope505_rows("intake_runs") == 0  # admitted locally, nothing dispatched yet
    restarted = world.worker()
    first = restarted.call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert first["created"] is False  # the crash already admitted it; no second draft
    ope505 = first["ope505"]
    assert ope505["state"] == "admitted" and ope505["run_state"] == "queued"
    assert ope505["benchmark_state"] == "draft" and ope505["benchmark_version"] == 1
    assert ope505["benchmark_ref"].startswith("benchmark-")
    again = world.worker().call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert again["ope505"] == ope505 and again["draft_key"] == first["draft_key"]
    # replaying the scan and re-freezing admits nothing new anywhere
    world.worker().call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.worker().call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert world.ope505_rows("intake_runs") == 1
    assert world.ope505_rows("intake_workflows") == 1
    assert world.ope505_rows("benchmarks") == 1
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        drafts = db.execute("SELECT count(*), sum(dispatched) FROM drafts").fetchone()
        ledger = db.execute("SELECT count(*) FROM ledger WHERE kind='ope505_admitted'").fetchone()[
            0
        ]
    assert tuple(drafts) == (1, 1) and ledger == 1
    # A crash after OPE-505 admitted the run but before the draft was marked dispatched makes
    # the next dispatch submit the same draft again: same run, same benchmark, one record.
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        key, payload = db.execute("SELECT draft_key, payload FROM drafts").fetchone()
    assert world.selection is not None
    sink = entry.StarterBenchmarkSink(world.selection, fx.TENANT, restarted.store)
    sink.create_draft(key, json.loads(payload))
    sink.enqueue_job("generated-job", key)
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("benchmarks") == 1
    assert len(restarted.store.ledger(sid, "ope505_admitted")) == 1
    with pytest.raises(entry.IntakeError) as unknown:
        sink.enqueue_job("generated-job", "generated-unknown-draft")
    assert unknown.value.code == "OPE505_ADMISSION_NOT_RECORDED"


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"revision": "generated-rev-01-0"}, "OPE505_GRANT_DOES_NOT_COVER_POLICY"),
        ({"policy": "generated-n05"}, "OPE505_GRANT_DOES_NOT_COVER_POLICY"),
        ({"tenant": "generated-tenant-b"}, "OPE505_TENANT_MISMATCH"),
    ],
)
def test_ope505_is_held_unless_the_setup_grant_names_the_frozen_policy(
    tmp_path: Path, override: dict[str, str], code: str
) -> None:
    world = World(tmp_path / "w")
    world.starter(**override)
    w, sid = scanned(world)
    held = w.call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": code}
    assert world.ope505_rows("intake_runs") == 0 and world.ope505_rows("benchmarks") == 0
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        assert db.execute("SELECT dispatched FROM drafts").fetchone()[0] == 0  # stays pending


def test_withdrawn_setup_grant_holds_dispatch_and_restoring_it_admits_once(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    selection = world.starter()
    withdrawn = selection.parent / "starter-intake-withdrawn-generated-starter.json"
    _private(withdrawn, {})
    w, sid = scanned(world)
    held = w.call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
    assert world.ope505_rows("intake_runs") == 0
    os.unlink(withdrawn)
    admitted = w.call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert admitted["ope505"]["state"] == "admitted"
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("benchmarks") == 1


def test_without_a_setup_grant_the_starter_is_frozen_but_not_dispatched(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    w, sid = scanned(world)
    result = w.call(OWNER, "mixed_intake_freeze_starter", {"session_id": sid})
    assert result["ope505"] == {"state": "not_configured", "reason": "starter_selection_absent"}
    assert result["created"] is True


# ------------------------------------------------------------------ triage comparison


def comparison_artifact(packet: Json) -> Json:
    by_ref = {row["ref"]: row for row in packet["dispositions"]}
    paths = {leaf[0]: leaf[4] for leaf in fx.LEAVES}
    entries = []
    for number, (native, fmt, role, state) in sorted(EXPECTED_DISPOSITIONS.items()):
        ref = public_ref(fx.TENANT, native) if state == "denied" else native
        row = by_ref[ref]
        observed = {
            "format": row["format"],
            "role": row["role"],
            "disposition": row["disposition"],
            "reason": row["reason"],
            "next_actor": row["next_actor"],
        }
        expected = {"format": fmt, "role": role, "disposition": state}
        entries.append(
            {
                "entry": number,
                "ref": ref,
                "fixture_sha256": (
                    hashlib.sha256(fx.FILES[paths[native]]).hexdigest() if native in paths else None
                ),
                "worker": observed,
                "expected": expected,
                "worker_matches_expected": all(observed[k] == v for k, v in expected.items()),
                "independent_reviewer": None,
            }
        )
    return {
        "schema_version": "opentour.mixed-intake.triage-comparison.v1",
        "issue": "OPE-531",
        "fixture_kind": "generated_sentinel",
        "evidence_tier": "simulated_unwitnessed",
        "connector_transport": "customer-local-directory",
        "installed_connector_coverage": False,
        "expected_source": "literal gold values from the OPE-531 acceptance text",
        "entries": entries,
        "independent_comparison": packet["independent_comparison"],
    }


def test_triage_comparison_artifact_on_the_generated_fixture(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    w, sid = scanned(world)
    packet = w.call(OWNER, "mixed_intake_triage_packet", {"session_id": sid})
    assert len(packet["dispositions"]) == 12
    artifact = comparison_artifact(packet)
    assert all(e["worker_matches_expected"] for e in artifact["entries"])
    # the comparison itself is outstanding: the worker never claims it
    assert artifact["independent_comparison"] == {
        "status": "outstanding",
        "reviewer": None,
        "reviewer_dispositions": None,
        "agreement": None,
    }
    assert all(e["independent_reviewer"] is None for e in artifact["entries"])
    assert fx.OUTSIDE_TARGET not in json.dumps(artifact)
    if os.environ.get("OPENTOUR_REGENERATE_TRIAGE_ARTIFACT") == "1":
        ARTIFACT.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    assert json.loads(ARTIFACT.read_text()) == artifact  # the committed artifact is reproducible


def test_triage_comparison_detects_a_disagreement(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    # The owner mis-declares entry 7's role; the comparison must surface it, not absorb it.
    world.record["connections"][LOCAL]["source_roles"]["generated-n07"] = "policy"
    world.save()
    w, sid = scanned(world)
    artifact = comparison_artifact(w.call(OWNER, "mixed_intake_triage_packet", {"session_id": sid}))
    mismatched = [e["entry"] for e in artifact["entries"] if not e["worker_matches_expected"]]
    assert mismatched == [7]


# ------------------------------------------------------------------ the stdio MCP entry


def test_stdio_mcp_entry_serves_the_same_tools_from_a_private_worker_directory(
    tmp_path: Path,
) -> None:
    world = World(tmp_path / "w")
    frames: list[Json] = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "generated", "version": "1"},
            },
        },
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "mixed_intake_preview",
                "arguments": {"connection_ref": LOCAL, "purpose": "policy"},
            },
        },
        {
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "mixed_intake_preview",
                "arguments": {"connection_ref": REMOTE, "purpose": "policy"},
            },
        },
    ]
    stdin = "".join(json.dumps({"jsonrpc": "2.0", **f}) + "\n" for f in frames)
    command = [
        sys.executable,
        "-m",
        "opentour_cell.pilot.mixed_intake.worker",
        "--directory",
        str(world.directory),
        "--actor",
        OWNER,
    ]
    done = subprocess.run(
        command, input=stdin, text=True, capture_output=True, timeout=60, check=True
    )
    replies = {r["id"]: r for r in map(json.loads, done.stdout.splitlines())}
    assert [t["name"] for t in replies[2]["result"]["tools"]] == list(entry.OPERATIONS)
    local_preview = replies[3]["result"]["structuredContent"]
    assert local_preview["content_reads"] == 0
    assert local_preview["connector_transport"] == "customer-local-directory"
    # the installed launcher has no connector transport: the MCP route is unavailable, not faked
    remote = replies[4]["result"]["structuredContent"]
    assert remote["status"] == "adapter_unavailable" and remote["base_starter_blocked"] is False
    stranger = subprocess.run(
        [*command[:-1], "generated-stranger"],
        input=stdin,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert stranger.returncode == 2 and stranger.stdout == ""
