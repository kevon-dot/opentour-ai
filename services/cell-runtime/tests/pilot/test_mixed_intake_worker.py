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
import threading
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
        self.grant_body: Json = {}
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
        self.selection = directory / "selection.json"
        _private(directory / "starter-intake-key.json", {"key": STARTER_KEY})
        self.grant_body = body
        path = self._seal(directory, body)
        self.record["starter_selection"] = str(path)
        self.save()
        return path

    @staticmethod
    def _seal(directory: Path, body: Json) -> Path:
        seal = hmac.digest(bytes.fromhex(STARTER_KEY), canonical(body).encode(), "sha256")
        _private(
            directory / f"starter-intake-grant-{body['grant_ref']}.json",
            {"grant": body, "seal_sha256": seal.hex()},
        )
        selection = {k: body[k] for k in ("tenant", "actor", "owner_id", "engagement_epoch")}
        path = directory / "selection.json"
        _private(
            path,
            {
                **selection,
                "schema_version": STARTER_SELECTION_VERSION,
                "grant_ref": body["grant_ref"],
            },
        )
        return path

    def regrant(self, grant_ref: str, **over: object) -> None:
        """The owner issues a fresh sealed setup grant in the same engagement."""
        assert self.selection is not None
        self.grant_body = {**self.grant_body, "grant_ref": grant_ref, **over}
        self.selection = self._seal(self.selection.parent, self.grant_body)

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
    assert set(ope505) == {
        "state",
        "draft_key",
        "run_ref",
        "correlation_ref",
        "run_state",
        "workflow_ref",
        "benchmark_ref",
        "benchmark_version",
        "benchmark_state",
        "predecessor_draft_key",
        "predecessor_run_ref",
        "lineage_parent_draft_key",
        "ope505_parent_run",
        "lineage_parent",
        "reconciled",
        "provenance",
    }
    assert ope505["reconciled"] is False
    assert ope505["provenance"] == "ope505_receipt_confirmed_at_record"
    assert ope505["lineage_parent"] is None
    assert ope505["predecessor_draft_key"] is None and ope505["ope505_parent_run"] is None
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


def ledger_rows(world: World, kind: str) -> list[tuple[str, Json]]:
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        rows = db.execute(
            "SELECT session_id, payload FROM ledger WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()
    return [(sid, json.loads(payload)) for sid, payload in rows]


FREEZE = "mixed_intake_freeze_starter"
RECEIPT_REFUSED = "ope505_receipt_refused"
NOT_COVERED = "OPE505_GRANT_DOES_NOT_COVER_POLICY"


def test_a_crash_after_ope505_admission_before_the_ledger_records_it_admits_once(
    tmp_path: Path, monkeypatch
) -> None:
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    class Crash(Exception):
        pass

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    original = BenchmarkIntake.get_run

    def crash_once(self, actor, run_ref):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(BenchmarkIntake, "get_run", original)
        raise Crash

    monkeypatch.setattr(BenchmarkIntake, "get_run", crash_once)
    with pytest.raises(Crash):
        w.call(OWNER, FREEZE, {"session_id": sid})
    assert world.ope505_rows("intake_runs") == 1 and ledger_rows(world, "ope505_admitted") == []
    restarted = world.worker()
    assert restarted.dispatch_pending()["admitted"] != []
    again = restarted.call(OWNER, FREEZE, {"session_id": sid})
    assert again["ope505"]["state"] == "admitted"
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("benchmarks") == 1
    # the re-submission reused the draft-derived idempotency key: no second OPE-505 key row
    assert world.ope505_rows("intake_keys") == 1
    assert len(ledger_rows(world, "ope505_admitted")) == 1
    assert restarted.dispatch_pending() == {"admitted": [], "reconciled": [], "held": []}


def test_f1_a_held_draft_never_wedges_unrelated_eligible_work(tmp_path: Path) -> None:
    """Review F1: one held draft must not stop every later freeze in the worker store."""
    world = World(tmp_path / "w", opaque=True)
    world.starter()
    w, narrow = scanned(world, deselect=["generated-n01"])
    held = w.call(OWNER, FREEZE, {"session_id": narrow})
    assert held["ope505"] == {"state": "held", "reason": NOT_COVERED}
    w, full = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": full})
    assert first["ope505"]["state"] == "admitted", first["ope505"]
    again = w.call(OWNER, FREEZE, {"session_id": full})
    assert again["ope505"] == first["ope505"]
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("benchmarks") == 1
    # the narrow freeze is still held for its own reason and stays pending
    assert w.call(OWNER, FREEZE, {"session_id": narrow})["ope505"] == held["ope505"]
    # the background sweep skips and records the held draft, and does not stop on it
    assert w.dispatch_pending() == {
        "admitted": [],
        "reconciled": [],
        "held": [{"draft_key": held["draft_key"], "reason": NOT_COVERED}],
    }
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        pending = db.execute("SELECT draft_key FROM drafts WHERE dispatched=0").fetchall()
    assert [k for (k,) in pending] == [held["draft_key"]]
    assert [p["draft_key"] for _, p in ledger_rows(world, "ope505_admitted")] == [
        first["draft_key"]
    ]
    # the denied name appears in no byte of a response, the worker store or the OPE-505 stores
    assert world.selection is not None
    dumps = [json.dumps([held, first, again])]
    for path in (
        world.directory / entry.STORE_FILE,
        world.selection.parent / "intake.sqlite3",
        world.selection.parent / "workflow-benchmarks.sqlite3",
    ):
        with sqlite3.connect(path) as db:
            dumps.append("\n".join(db.iterdump()))
    assert all(fx.RESTRICTED_NAME not in dump for dump in dumps)


def test_f2_dispatch_never_submits_another_sessions_draft_whose_grant_was_removed(
    tmp_path: Path,
) -> None:
    """Review F2: a draft frozen under a connection grant the owner removed is never submitted."""
    world = World(tmp_path / "w")
    w, remote = scanned(world, REMOTE)
    parked = w.call(OWNER, FREEZE, {"session_id": remote})
    assert parked["ope505"]["state"] == "not_configured"
    del world.record["connections"][REMOTE]
    world.starter()
    assert refused(w, OWNER, FREEZE, {"session_id": remote}) == "CONNECTION_NOT_GRANTED"
    w, local = scanned(world)
    frozen = w.call(OWNER, FREEZE, {"session_id": local})
    assert frozen["ope505"]["state"] == "admitted"
    admitted = ledger_rows(world, "ope505_admitted")
    assert [(sid, p["draft_key"]) for sid, p in admitted] == [(local, frozen["draft_key"])]
    assert w.dispatch_pending() == {
        "admitted": [],
        "reconciled": [],
        "held": [{"draft_key": parked["draft_key"], "reason": "CONNECTION_NOT_GRANTED"}],
    }
    assert len(ledger_rows(world, "ope505_admitted")) == 1
    assert world.ope505_rows("intake_runs") == 1


def test_f2_dispatch_never_submits_a_draft_whose_session_grant_changed(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    w, old = scanned(world)
    parked = w.call(OWNER, FREEZE, {"session_id": old})
    world.record["connections"][LOCAL]["deselected"] = ["generated-n07"]
    world.starter()
    assert refused(w, OWNER, FREEZE, {"session_id": old}) == "GRANT_CHANGED"
    w, new = scanned(world)
    frozen = w.call(OWNER, FREEZE, {"session_id": new})
    assert frozen["draft_key"] != parked["draft_key"]
    assert frozen["ope505"]["state"] == "admitted"
    admitted = ledger_rows(world, "ope505_admitted")
    assert [(sid, p["draft_key"]) for sid, p in admitted] == [(new, frozen["draft_key"])]
    assert w.dispatch_pending()["held"] == [
        {"draft_key": parked["draft_key"], "reason": "GRANT_CHANGED"}
    ]


def test_f2_a_draft_is_never_dispatched_under_another_tenants_setup_grant(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    w, tenant_a = scanned(world)
    parked = w.call(OWNER, FREEZE, {"session_id": tenant_a})
    # The owner moves this worker to tenant B: a tenant-B manifest and a tenant-B setup grant.
    other = "generated-tenant-b"
    world.record["tenant"] = other
    world.save()  # the connector still reports tenant A: refused before any scan
    for op in ("mixed_intake_preview", "mixed_intake_open"):
        assert refused(w, OWNER, op, {"connection_ref": LOCAL, "purpose": "policy"}) == (
            "CONNECTION_MISMATCH"
        )
    manifest = world.fixture.manifest()
    manifest["tenant"] = other
    path_b = world.base / "manifest-b.json"
    path_b.write_text(json.dumps(manifest))
    world.record["tenant"] = other
    world.record["connections"][LOCAL]["manifest"] = str(path_b)
    world.starter(tenant=other)
    w, tenant_b = scanned(world)
    frozen = w.call(OWNER, FREEZE, {"session_id": tenant_b})
    assert frozen["ope505"]["state"] == "admitted"
    admitted = ledger_rows(world, "ope505_admitted")
    assert [(sid, p["draft_key"]) for sid, p in admitted] == [(tenant_b, frozen["draft_key"])]
    assert w.dispatch_pending()["held"] == [
        {"draft_key": parked["draft_key"], "reason": "GRANT_CHANGED"}
    ]
    # the sink itself compares the principal with the draft's own tenant, not the record's
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        payload = db.execute(
            "SELECT payload FROM drafts WHERE draft_key=?", (parked["draft_key"],)
        ).fetchone()[0]
    assert world.selection is not None
    sink = entry.StarterBenchmarkSink(world.selection, other, w.store)
    with pytest.raises(entry.IntakeError) as mismatch:
        sink.create_draft(parked["draft_key"], json.loads(payload))
    assert mismatch.value.code == "OPE505_TENANT_MISMATCH"
    assert len(ledger_rows(world, "ope505_admitted")) == 1


def test_f3_reopening_the_same_evidence_reports_the_existing_admission(tmp_path: Path) -> None:
    """Review F3: the OPE-505 ledger is keyed by draft, not by the first session."""
    world = World(tmp_path / "w")
    world.starter()
    w, first_sid = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": first_sid})
    assert first["ope505"]["state"] == "admitted"
    world.record["connections"][LOCAL]["page_size"] = 4  # forces a new session, same evidence
    world.save()
    assert refused(w, OWNER, "mixed_intake_run", {"session_id": first_sid}) == "GRANT_CHANGED"
    w, second_sid = scanned(world)
    assert second_sid != first_sid
    second = w.call(OWNER, FREEZE, {"session_id": second_sid})
    assert second["draft_key"] == first["draft_key"] and second["created"] is False
    assert second["ope505"] == first["ope505"]
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("benchmarks") == 1


def test_f4_a_successor_is_never_reported_on_its_predecessors_ope505_run(tmp_path: Path) -> None:
    """Review F4: fresh evidence creates a linked successor, never a collapsed admission."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": sid})
    assert first["ope505"]["state"] == "admitted" and first["ope505"]["benchmark_version"] == 1
    world.fixture.edit(
        "generated-n05",
        fx.CSV_DECISIONS + b"00419,2026-01-12T10:00:00+00:00,approve,0300\n",
        "generated-rev-05-2",
    )
    world.fixture.write_manifest()
    assert w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})[
        "status"
    ] == ("sealed")
    second = w.call(OWNER, FREEZE, {"session_id": sid})
    assert second["supersedes"] == first["draft_key"] and second["created"] is True
    # The sealed setup grant fixes the OPE-505 run identity, so under the same grant the
    # successor is held and nothing is submitted (no new OPE-505 key, run or benchmark).
    assert second["ope505"] == {
        "state": "held",
        "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT",
    }
    assert world.ope505_rows("intake_keys") == 1 and world.ope505_rows("intake_runs") == 1
    holds = [p for _, p in ledger_rows(world, "dispatch_held")]
    assert holds[-1] == {
        "draft_key": second["draft_key"],
        "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT",
        "predecessor_draft_key": first["draft_key"],
        "predecessor_run_ref": first["ope505"]["run_ref"],
    }
    # A fresh owner grant admits the successor as a new OPE-505 run, linked to its predecessor
    # by OPE-505's own benchmark lineage (version 2, parent run = the predecessor's run).
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    third = w.call(OWNER, FREEZE, {"session_id": sid})
    assert third["draft_key"] == second["draft_key"]
    ope505 = third["ope505"]
    assert ope505["state"] == "admitted"
    assert ope505["run_ref"] != first["ope505"]["run_ref"]
    assert ope505["benchmark_ref"] != first["ope505"]["benchmark_ref"]
    assert ope505["benchmark_version"] == 2
    assert ope505["predecessor_draft_key"] == first["draft_key"]
    assert ope505["predecessor_run_ref"] == first["ope505"]["run_ref"]
    assert ope505["lineage_parent_draft_key"] == first["draft_key"]
    assert ope505["ope505_parent_run"] == first["ope505"]["run_ref"]
    assert world.ope505_rows("intake_runs") == 2 and world.ope505_rows("benchmarks") == 2


def test_an_unrelated_draft_is_never_reported_on_another_drafts_run(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    world.starter()
    w, full = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": full})
    assert first["ope505"]["state"] == "admitted"
    w, narrow = scanned(world, deselect=["generated-n07"])
    other = w.call(OWNER, FREEZE, {"session_id": narrow})
    assert other["supersedes"] is None and other["draft_key"] != first["draft_key"]
    assert other["ope505"] == {"state": "held", "reason": "OPE505_RUN_BOUND_TO_OTHER_DRAFT"}
    assert world.ope505_rows("intake_keys") == 1 and world.ope505_rows("intake_runs") == 1
    # Re-review R5: a fresh owner grant never makes an unrelated draft v2 of another draft's run.
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    again = w.call(OWNER, FREEZE, {"session_id": narrow})
    assert again["ope505"] == {"state": "held", "reason": "OPE505_RUN_BOUND_TO_OTHER_DRAFT"}
    assert w.dispatch_pending()["held"] == [
        {"draft_key": other["draft_key"], "reason": "OPE505_RUN_BOUND_TO_OTHER_DRAFT"}
    ]
    assert world.ope505_rows("intake_keys") == 1 and world.ope505_rows("intake_runs") == 1
    assert world.ope505_rows("benchmarks") == 1


def test_r6_a_superseded_draft_is_never_dispatched(tmp_path: Path) -> None:
    """Re-review R6: stale evidence never becomes the newest OPE-505 benchmark."""
    world = World(tmp_path / "w")
    selection = world.starter()
    withdrawn = selection.parent / "starter-intake-withdrawn-generated-starter.json"
    _private(withdrawn, {})
    w, sid = scanned(world)
    stale = w.call(OWNER, FREEZE, {"session_id": sid})
    assert stale["ope505"] == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
    world.fixture.edit(
        "generated-n05",
        fx.CSV_DECISIONS + b"00419,2026-01-12T10:00:00+00:00,approve,0300\n",
        "generated-rev-05-2",
    )
    world.fixture.write_manifest()
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    fresh = w.call(OWNER, FREEZE, {"session_id": sid})
    assert fresh["supersedes"] == stale["draft_key"]
    assert fresh["ope505"]["state"] == "admitted" and fresh["ope505"]["benchmark_version"] == 1
    # superseded is permanent and independent of the grant: reported even with no grant open
    second = world.selection.parent / "starter-intake-withdrawn-generated-starter-2.json"
    _private(second, {})
    assert w.dispatch_pending()["held"] == [
        {"draft_key": stale["draft_key"], "reason": "DRAFT_SUPERSEDED"}
    ]
    os.unlink(second)
    # the original grant comes back: the superseded predecessor is still never dispatched
    world.regrant("generated-starter", setup_policy_revision="generated-setup-rev-1")
    os.unlink(withdrawn)
    assert w.dispatch_pending() == {
        "admitted": [],
        "reconciled": [],
        "held": [{"draft_key": stale["draft_key"], "reason": "DRAFT_SUPERSEDED"}],
    }
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("benchmarks") == 1
    assert [p["draft_key"] for _, p in ledger_rows(world, "ope505_admitted")] == [
        fresh["draft_key"]
    ]
    assert fresh["ope505"]["lineage_parent_draft_key"] is None


def gate_get_run(monkeypatch, parties: int = 2) -> threading.Barrier:
    """Hold every dispatch at `BenchmarkIntake.get_run` until all parties have reached it."""
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    barrier = threading.Barrier(parties, timeout=30)
    original = BenchmarkIntake.get_run

    def gated(self, actor, run_ref):  # type: ignore[no-untyped-def]
        barrier.wait()
        return original(self, actor, run_ref)

    monkeypatch.setattr(BenchmarkIntake, "get_run", gated)
    return barrier


def concurrently(world: World, *sessions: str) -> list[Json]:
    results: dict[int, Json] = {}
    errors: list[BaseException] = []
    # Opened up front: opening a worker takes the store's write lock briefly (its indexes).
    workers = [world.worker() for _ in sessions]

    def freeze(index: int, sid: str) -> None:
        try:
            results[index] = workers[index].call(OWNER, FREEZE, {"session_id": sid})
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=freeze, args=(i, sid)) for i, sid in enumerate(sessions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == [] and len(results) == len(sessions), errors
    return [results[i] for i in range(len(sessions))]


def test_g1_concurrent_freezes_of_two_drafts_bind_one_run_to_one_draft(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review G1 repro A: both dispatches pass the pre-check before either records."""
    world = World(tmp_path / "w")
    world.starter()
    _, full = scanned(world)
    _, narrow = scanned(world, deselect=["generated-n07"])
    gate_get_run(monkeypatch)
    results = concurrently(world, full, narrow)
    states = sorted((r["ope505"]["state"], r["ope505"].get("reason")) for r in results)
    assert states == [("admitted", None), ("held", "OPE505_RUN_BOUND_TO_OTHER_DRAFT")]
    winner = next(r for r in results if r["ope505"]["state"] == "admitted")
    admitted = [p for _, p in ledger_rows(world, "ope505_admitted")]
    assert [p["draft_key"] for p in admitted] == [winner["draft_key"]]
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("benchmarks") == 1


def test_g1_concurrent_freezes_of_one_draft_record_one_admission(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review G1 repro B: two sessions differing only by purpose share one draft."""
    world = World(tmp_path / "w")
    world.starter()
    w = world.worker()
    sids = [
        w.call(OWNER, "mixed_intake_open", {"connection_ref": LOCAL, "purpose": purpose})[
            "session_id"
        ]
        for purpose in ("policy", "historical_decision_traces")
    ]
    assert sids[0] != sids[1]
    for sid in sids:
        assert w.call(OWNER, "mixed_intake_run", {"session_id": sid})["status"] == "sealed"
    gate_get_run(monkeypatch)
    results = concurrently(world, *sids)
    assert results[0]["draft_key"] == results[1]["draft_key"]
    assert sorted(r["created"] for r in results) == [False, True]  # no IntegrityError race
    # one draft, one admission: both sessions read the same recorded admission
    assert results[0]["ope505"]["state"] == "admitted"
    assert results[0]["ope505"] == results[1]["ope505"]
    assert len(ledger_rows(world, "ope505_admitted")) == 1
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        assert db.execute("SELECT count(*), sum(dispatched) FROM drafts").fetchone() == (1, 1)
    assert world.ope505_rows("intake_runs") == 1


def test_a_draft_superseded_while_its_dispatch_is_in_flight_is_never_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review A5 (threads): superseded after its submit, the stale draft is never adopted.

    The stale dispatch is held at `get_run`, after OPE-505 admitted its run and its receipt was
    journaled. Under the same grant the successor would resolve to that same run, so it is held
    before submitting; the stale draft is held as `DRAFT_SUPERSEDED`, with its refusal marker.
    A fresh grant admits the successor on its own, distinct run, whose parent is the stale run.
    """
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    finish = in_flight_at_get_run(monkeypatch, world, sid)
    assert len(ledger_rows(world, "ope505_receipt")) == 1
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    fresh = w.call(OWNER, FREEZE, {"session_id": sid})
    stale = finish()
    assert fresh["supersedes"] == stale["draft_key"]
    assert fresh["ope505"] == {"state": "held", "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT"}
    assert stale["ope505"] == {"state": "held", "reason": "DRAFT_SUPERSEDED"}
    assert ledger_rows(world, "ope505_admitted") == []
    assert [(p["draft_key"], p["reason"]) for _, p in ledger_rows(world, RECEIPT_REFUSED)] == [
        (stale["draft_key"], "DRAFT_SUPERSEDED")
    ]
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("intake_keys") == 1
    assert_successor_on_its_own_run(world, w, sid, fresh, stale)


def assert_successor_on_its_own_run(world: World, w, sid: str, fresh: Json, stale: Json) -> None:  # type: ignore[no-untyped-def]
    """A fresh grant admits the successor on a distinct run whose parent is the stale run."""
    stale_run = next(
        p["run_ref"]
        for _, p in ledger_rows(world, "ope505_receipt")
        if p["draft_key"] == stale["draft_key"]
    )
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    ope505 = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert ope505["state"] == "admitted" and ope505["draft_key"] == fresh["draft_key"]
    assert ope505["run_ref"] != stale_run  # never recorded on the ancestor's run
    assert ope505["ope505_parent_run"] == stale_run and ope505["benchmark_version"] == 2
    assert ope505["lineage_parent"] == {
        "draft_key": stale["draft_key"],
        "run_ref": stale_run,
        "basis": "own_receipt",
        "state": "superseded_not_admitted",
    }
    own = [
        p for _, p in ledger_rows(world, "ope505_receipt") if p["draft_key"] == fresh["draft_key"]
    ]
    assert [p["run_ref"] for p in own] == [ope505["run_ref"]]  # created by its own keyed submit
    assert [p["draft_key"] for _, p in ledger_rows(world, "ope505_admitted")] == [
        fresh["draft_key"]
    ]
    assert world.ope505_rows("intake_runs") == 2
    assert w.dispatch_pending()["held"] == [
        {"draft_key": stale["draft_key"], "reason": "DRAFT_SUPERSEDED"}
    ]


GATED_FREEZE = """
import json, pathlib, sys, time
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake
from opentour_cell.pilot.mixed_intake import worker as entry

directory, gate, actor, sid = sys.argv[1:5]
gate = pathlib.Path(gate)
original = BenchmarkIntake.get_run

def gated(self, principal, run_ref):
    (gate / "reached").touch()
    for _ in range(1200):
        if (gate / "release").exists():
            break
        time.sleep(0.05)
    return original(self, principal, run_ref)

BenchmarkIntake.get_run = gated
worker = entry.MixedIntakeWorker(pathlib.Path(directory))
print(json.dumps(worker.call(actor, "mixed_intake_freeze_starter", {"session_id": sid})))
"""


def test_a5_a_draft_superseded_in_another_process_is_never_adopted(tmp_path: Path) -> None:
    """Re-review A5 (processes): the stale freeze runs in a separate process on one directory."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    gate = tmp_path / "gate"
    gate.mkdir()
    stale_process = subprocess.Popen(
        [sys.executable, "-c", GATED_FREEZE, str(world.directory), str(gate), OWNER, sid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(1200):
        if (gate / "reached").exists() or stale_process.poll() is not None:
            break
        time.sleep(0.05)
    assert (gate / "reached").exists(), stale_process.communicate()
    assert len(ledger_rows(world, "ope505_receipt")) == 1
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    fresh = w.call(OWNER, FREEZE, {"session_id": sid})
    (gate / "release").touch()
    out, err = stale_process.communicate(timeout=120)
    assert stale_process.returncode == 0, err
    stale = json.loads(out)
    assert (
        fresh["ope505"] == {"state": "held", "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT"}
        and fresh["supersedes"] == stale["draft_key"]
    )
    assert stale["ope505"] == {"state": "held", "reason": "DRAFT_SUPERSEDED"}
    assert ledger_rows(world, "ope505_admitted") == [] and world.ope505_rows("intake_runs") == 1
    assert_successor_on_its_own_run(world, w, sid, fresh, stale)


class _Hooked(sqlite3.Connection):
    """Runs `HOOK` before each statement, so a test can hold one writer at an exact point."""

    HOOK: list = []

    def execute(self, sql, *args):  # type: ignore[no-untyped-def, override]
        for hook in self.HOOK:
            hook(sql)
        return super().execute(sql, *args)


def test_g1_concurrent_freezes_of_one_evidence_never_race_the_draft_insert(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review G1: the existence check and the draft insert are one serialized step."""
    world = World(tmp_path / "w")
    w = world.worker()
    sids = [
        w.call(OWNER, "mixed_intake_open", {"connection_ref": LOCAL, "purpose": purpose})[
            "session_id"
        ]
        for purpose in ("policy", "historical_decision_traces")
    ]
    for sid in sids:
        w.call(OWNER, "mixed_intake_run", {"session_id": sid})
    checked, both_loaded, waited = threading.Event(), threading.Event(), []
    lock = threading.Lock()
    seen, loaded = [0], set()

    def hook(sql: str) -> None:
        if sql.startswith("SELECT * FROM sessions WHERE session_id=?"):
            with lock:  # each freeze loads its session just before its draft transaction
                loaded.add(threading.get_ident())
                if len(loaded) == 2:
                    both_loaded.set()
        elif sql.startswith("SELECT 1 FROM drafts WHERE draft_key=?"):
            with lock:
                seen[0] += 1
                if seen[0] == 2:
                    checked.set()
        elif sql.startswith("SELECT draft_key FROM drafts WHERE session_id=?"):
            with lock:
                first = not waited
                waited.append(sql)
            if first:
                # Both freezes are under way. Serialized, the other one is blocked before its
                # existence check until this one commits, so this wait times out.
                assert both_loaded.wait(30)
                checked.wait(2.5)

    real = sqlite3.connect
    monkeypatch.setattr(_Hooked, "HOOK", [hook])
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real(*a, **{"factory": _Hooked, **kw}))
    results = concurrently(world, *sids)
    assert sorted(r["created"] for r in results) == [False, True]
    assert results[0]["draft_key"] == results[1]["draft_key"]
    with real(world.directory / entry.STORE_FILE) as db:
        assert db.execute("SELECT count(*) FROM drafts").fetchone() == (1,)


def test_g1_the_recording_transaction_serializes_concurrent_dispatchers(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review G1: the final checks and the ledger write hold the store's write lock."""
    world = World(tmp_path / "w")
    world.starter()
    _, full = scanned(world)
    _, narrow = scanned(world, deselect=["generated-n07"])
    gate_get_run(monkeypatch)
    original = entry.StarterBenchmarkSink._lineage
    both_checked, checked = threading.Event(), [0]
    lock = threading.Lock()

    def lineage(db, draft_key, identity, workflow, link, run_ref=None):  # type: ignore[no-untyped-def]
        parent = original(db, draft_key, identity, workflow, link, run_ref)
        if run_ref is not None:
            with lock:
                checked[0] += 1
                if checked[0] == 2:
                    both_checked.set()
            # Unserialized, both dispatchers would pass their checks before either writes.
            # Serialized, the other cannot check until this one commits, so this times out.
            both_checked.wait(2.5)
        return parent

    monkeypatch.setattr(entry.StarterBenchmarkSink, "_lineage", staticmethod(lineage))
    results = concurrently(world, full, narrow)
    states = sorted((r["ope505"]["state"], r["ope505"].get("reason")) for r in results)
    assert states == [("admitted", None), ("held", "OPE505_RUN_BOUND_TO_OTHER_DRAFT")]
    assert len(ledger_rows(world, "ope505_admitted")) == 1


def test_a_recorded_admission_is_marked_dispatched_even_after_the_grant_is_withdrawn(
    tmp_path: Path,
) -> None:
    """A crash after the admission was recorded, before the draft was marked dispatched."""
    world = World(tmp_path / "w")
    selection = world.starter()
    w, sid = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": sid})
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        db.execute("UPDATE drafts SET dispatched=0")
        db.execute("DELETE FROM jobs")
    _private(selection.parent / "starter-intake-withdrawn-generated-starter.json", {})
    assert w.dispatch_pending() == {"admitted": [first["draft_key"]], "reconciled": [], "held": []}
    assert world.ope505_rows("intake_keys") == 1 and len(ledger_rows(world, "ope505_admitted")) == 1
    # with the grant back, a recorded draft never even reopens the grant or reaches OPE-505
    os.unlink(selection.parent / "starter-intake-withdrawn-generated-starter.json")
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        db.execute("UPDATE drafts SET dispatched=0")
    opened: list[object] = []
    original = entry.StarterBenchmarkSink._open

    def count(self):  # type: ignore[no-untyped-def]
        opened.append(self)
        return original(self)

    entry.StarterBenchmarkSink._open = count  # type: ignore[method-assign]
    try:
        assert w.dispatch_pending() == {
            "admitted": [first["draft_key"]],
            "reconciled": [],
            "held": [],
        }
    finally:
        entry.StarterBenchmarkSink._open = original  # type: ignore[method-assign]
    assert opened == []


def test_the_worker_store_enforces_one_admission_per_draft_and_per_run(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": sid})
    entry_row = dict(first["ope505"], kind=None)
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        for clash in (
            {**entry_row, "draft_key": "generated-other-draft"},
            {**entry_row, "run_ref": "run-generated-other"},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO ledger(session_id, kind, payload) VALUES (?,?,?)",
                    (sid, "ope505_admitted", json.dumps(clash)),
                )


def test_g3_a_record_change_during_dispatch_is_held_not_admitted(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review G3: the record is re-read after the grant opens and inside the commit."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    original_open = entry.StarterBenchmarkSink._open

    def change_then_open(self):  # type: ignore[no-untyped-def]
        world.record["connections"][LOCAL]["source_roles"]["generated-n07"] = "policy"
        world.save()
        return original_open(self)

    monkeypatch.setattr(entry.StarterBenchmarkSink, "_open", change_then_open)
    held = w.call(OWNER, FREEZE, {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "WORKER_RECORD_CHANGED"}
    assert world.ope505_rows("intake_runs") == 0 and ledger_rows(world, "ope505_admitted") == []


def test_g3_a_record_change_after_submit_is_never_recorded_as_admitted(
    tmp_path: Path, monkeypatch
) -> None:
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    original = BenchmarkIntake.get_run
    saved = json.loads(json.dumps(world.record))

    def remove_connection(self, actor, run_ref):  # type: ignore[no-untyped-def]
        del world.record["connections"][LOCAL]
        world.save()
        return original(self, actor, run_ref)

    monkeypatch.setattr(BenchmarkIntake, "get_run", remove_connection)
    held = w.call(OWNER, FREEZE, {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "WORKER_RECORD_CHANGED"}
    assert ledger_rows(world, "ope505_admitted") == []
    monkeypatch.undo()
    world.record = saved
    world.save()
    admitted = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert admitted["state"] == "admitted" and world.ope505_rows("intake_runs") == 1
    # under the same intent the retry resubmits and records the full admission, not an adoption
    assert admitted["reconciled"] is False and admitted["benchmark_ref"] is not None


def test_a_run_ope505_links_to_a_run_outside_the_local_chain_is_not_claimed(
    tmp_path: Path,
) -> None:
    """Lineage follows the local chain: OPE-505's parent must be the chain's admitted run."""
    from opentour_cell.pilot.intake_local import open_intake
    from opentour_cell.pilot.intake_mcp import Starter

    world = World(tmp_path / "w")
    selection = world.starter()
    principal, admission = open_intake(selection)
    body = world.grant_body
    outside = admission.submit_intake(
        principal,
        Starter(
            policy_resource_ref=body["policy_resource_ref"],
            source_revision=body["source_revision"],
            setup_policy_revision=body["setup_policy_revision"],
            idempotency_key="generated-outside-client",
            creation_ref=body["creation_ref"],
            workflow_ref=None,
        ),
    )
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    w, sid = scanned(world)
    held = w.call(OWNER, FREEZE, {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}
    assert ledger_rows(world, "ope505_admitted") == []
    assert outside.run_ref  # the outside run stays the only lineage OPE-505 knows
    # a receipt refused for its lineage is never adopted, even for a chain successor
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-3", setup_policy_revision="generated-setup-rev-3")
    successor = w.call(OWNER, FREEZE, {"session_id": sid})
    assert successor["supersedes"] == held["draft_key"]
    assert successor["ope505"] == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}
    assert ledger_rows(world, "ope505_admitted") == []


def edit_n05(world: World, revision: str) -> None:
    extra = {"generated-rev-05-2": b"00419", "generated-rev-05-3": b"00420"}[revision]
    world.fixture.edit(
        "generated-n05",
        fx.CSV_DECISIONS + extra + b",2026-01-12T10:00:00+00:00,approve,0300\n",
        revision,
    )
    world.fixture.write_manifest()


def in_flight_at_get_run(monkeypatch, world: World, sid: str):  # type: ignore[no-untyped-def]
    """Start a freeze of `sid` and hold it after OPE-505 admitted its run (receipt journaled)."""
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    reached, release = threading.Event(), threading.Event()
    original = BenchmarkIntake.get_run
    calls: list[str] = []

    def first_waits(self, actor, run_ref):  # type: ignore[no-untyped-def]
        calls.append(run_ref)
        if len(calls) == 1:
            reached.set()
            assert release.wait(30)
        return original(self, actor, run_ref)

    monkeypatch.setattr(BenchmarkIntake, "get_run", first_waits)
    results: list[Json] = []
    thread = threading.Thread(
        target=lambda: results.append(world.worker().call(OWNER, FREEZE, {"session_id": sid}))
    )
    thread.start()
    assert reached.wait(30)

    def finish() -> Json:
        release.set()
        thread.join(60)
        return results[0]

    return finish


@pytest.mark.parametrize("tamper", [None, "key", "workflow"])
def test_x3_a_held_in_flight_ancestor_is_reconciled_before_its_successor(
    tmp_path: Path, monkeypatch, tamper: str | None
) -> None:
    """Owner decision M1, repro X3 with a regrant: adopt the worker's own receipt, never more."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    finish = in_flight_at_get_run(monkeypatch, world, sid)
    if tamper == "key":
        with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
            (row_id, payload) = db.execute(
                "SELECT id, payload FROM ledger WHERE kind='ope505_receipt'"
            ).fetchone()
            forged = {**json.loads(payload), "idempotency_key": "generated-not-derived"}
            db.execute("UPDATE ledger SET payload=? WHERE id=?", (json.dumps(forged), row_id))
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    over = {"setup_policy_revision": "generated-setup-rev-2"}
    if tamper == "workflow":
        over["creation_ref"] = "generated-creation-2"  # a different OPE-505 workflow
    world.regrant("generated-starter-2", **over)
    successor = w.call(OWNER, FREEZE, {"session_id": sid})
    stale = finish()
    assert successor["supersedes"] == stale["draft_key"]
    admitted = {p["draft_key"]: p for _, p in ledger_rows(world, "ope505_admitted")}
    assert world.ope505_rows("intake_runs") == 2  # never a duplicate run
    if tamper is None:
        # The superseded ancestor is never adopted (A5). Its own verified receipt is the
        # successor's expected OPE-505 parent, so the successor is v2 on its own run.
        r1 = ledger_rows(world, "ope505_receipt")[0][1]
        assert stale["draft_key"] not in admitted
        assert stale["ope505"] == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
        ope505 = successor["ope505"]
        assert ope505["state"] == "admitted" and ope505["reconciled"] is False
        assert ope505["benchmark_version"] == 2
        assert ope505["ope505_parent_run"] == r1["run_ref"] != ope505["run_ref"]
        assert ope505["lineage_parent_draft_key"] == stale["draft_key"]
        assert ope505["lineage_parent"] == {
            "draft_key": stale["draft_key"],
            "run_ref": r1["run_ref"],
            "basis": "own_receipt",
            "state": "superseded_not_admitted",
        }
        assert world.ope505_rows("benchmarks") == 2
        assert w.dispatch_pending()["held"] == [
            {"draft_key": stale["draft_key"], "reason": "DRAFT_SUPERSEDED"}
        ]
    elif tamper == "key":
        # never adopt a run whose key the worker did not derive for that draft
        assert stale["draft_key"] not in admitted
        assert successor["ope505"] == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}
        assert stale["ope505"] == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
    else:
        # never adopt across workflows: the successor starts the new workflow at v1
        assert stale["draft_key"] not in admitted
        assert successor["ope505"]["state"] == "admitted"
        assert successor["ope505"]["benchmark_version"] == 1
        assert successor["ope505"]["ope505_parent_run"] is None


@pytest.mark.parametrize("same_workflow", [True, False])
def test_x7_a_record_change_hold_then_regrant_reconciles_or_holds_typed(
    tmp_path: Path, monkeypatch, same_workflow: bool
) -> None:
    """Owner decision M1, repro X7: the retry adopts its receipt, or names the moved intent."""
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    saved = json.loads(json.dumps(world.record))
    original = BenchmarkIntake.get_run

    def change_record(self, actor, run_ref):  # type: ignore[no-untyped-def]
        world.record["connections"][LOCAL]["page_size"] = 7
        world.save()
        return original(self, actor, run_ref)

    monkeypatch.setattr(BenchmarkIntake, "get_run", change_record)
    held = w.call(OWNER, FREEZE, {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "WORKER_RECORD_CHANGED"}
    monkeypatch.undo()
    world.record = saved
    world.save()
    over = {"setup_policy_revision": "generated-setup-rev-2"}
    if not same_workflow:
        over["creation_ref"] = "generated-creation-2"
    world.regrant("generated-starter-2", **over)
    retry = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert world.ope505_rows("intake_runs") == 1
    if not same_workflow:
        assert retry == {"state": "held", "reason": "OPE505_INTENT_CHANGED"}
        return
    assert retry["state"] == "admitted" and retry["reconciled"] is True
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-3", setup_policy_revision="generated-setup-rev-3")
    successor = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert successor["state"] == "admitted" and successor["benchmark_version"] == 2
    assert successor["ope505_parent_run"] == retry["run_ref"]
    assert world.ope505_rows("intake_runs") == 2


def outside_run(world: World) -> str:
    """An OPE-505 client outside this worker admits a run in the grant's workflow."""
    from opentour_cell.pilot.intake_local import open_intake
    from opentour_cell.pilot.intake_mcp import Starter

    assert world.selection is not None
    principal, admission = open_intake(world.selection)
    body = world.grant_body
    receipt = admission.submit_intake(
        principal,
        Starter(
            policy_resource_ref=body["policy_resource_ref"],
            source_revision=body["source_revision"],
            setup_policy_revision=body["setup_policy_revision"],
            idempotency_key="generated-outside-client",
            creation_ref=body["creation_ref"],
            workflow_ref=None,
        ),
    )
    return str(receipt.run_ref)  # type: ignore[union-attr]


def held_on_record_change(monkeypatch, world: World, w, sid: str) -> Json:  # type: ignore[no-untyped-def]
    """Freeze `sid` with the record changing during `get_run`: held after the receipt."""
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    saved = json.loads(json.dumps(world.record))
    original = BenchmarkIntake.get_run

    def change_record(self, actor, run_ref):  # type: ignore[no-untyped-def]
        world.record["connections"][LOCAL]["page_size"] = 7
        world.save()
        return original(self, actor, run_ref)

    monkeypatch.setattr(BenchmarkIntake, "get_run", change_record)
    held = w.call(OWNER, FREEZE, {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "WORKER_RECORD_CHANGED"}
    monkeypatch.setattr(BenchmarkIntake, "get_run", original)
    world.record = saved
    world.save()
    return held


@pytest.mark.parametrize("guarded", [True, False])
def test_a1_adoption_requires_ope505_parent_to_be_the_expected_parent(
    tmp_path: Path, monkeypatch, guarded: bool
) -> None:
    """Re-review A1: a receipt whose OPE-505 parent is an outside run is never adopted.

    With `guarded=False` the parent comparison is patched out, and the same repro adopts the
    receipt with an outside parent: the comparison is what holds it.
    """
    world = World(tmp_path / "w")
    world.starter()
    r0 = outside_run(world)
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    w, sid = scanned(world)
    held = held_on_record_change(monkeypatch, world, w, sid)
    receipt = ledger_rows(world, "ope505_receipt")[0][1]
    assert receipt["lineage"]["parent_run"] == r0  # OPE-505's own parent, captured at submit
    assert receipt["lineage"]["benchmark_version"] == 2
    world.regrant("generated-starter-3", setup_policy_revision="generated-setup-rev-3")
    if not guarded:
        monkeypatch.setattr(entry, "parent_matches", lambda lineage, expected: True)
    retry = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert world.ope505_rows("intake_runs") == 2  # R0 and R1; nothing new was submitted
    if not guarded:
        assert retry["state"] == "admitted" and retry["ope505_parent_run"] == r0
        return
    assert retry == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}
    assert ledger_rows(world, "ope505_admitted") == []
    assert [(p["draft_key"], p["reason"]) for _, p in ledger_rows(world, RECEIPT_REFUSED)] == [
        (held["draft_key"], "OPE505_LINEAGE_MISMATCH")
    ]


def test_a1b_a_crash_between_the_refusal_and_its_marker_leaves_the_draft_unadoptable(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-review A1b: the marker commits with the refusal, and adoption re-derives it anyway."""
    from opentour_cell.pilot.mixed_intake.store import IntakeStore

    class Crash(Exception):
        pass

    world = World(tmp_path / "w")
    world.starter()
    outside_run(world)
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    w, sid = scanned(world)
    original = IntakeStore.log

    def crash_at_marker(db, session_id, kind, payload):  # type: ignore[no-untyped-def]
        if kind == RECEIPT_REFUSED:
            raise Crash
        return original(db, session_id, kind, payload)

    monkeypatch.setattr(IntakeStore, "log", staticmethod(crash_at_marker))
    with pytest.raises(Crash):
        w.call(OWNER, FREEZE, {"session_id": sid})
    monkeypatch.setattr(IntakeStore, "log", staticmethod(original))
    # the refusal and its marker are one transaction: neither committed, nothing admitted
    assert ledger_rows(world, RECEIPT_REFUSED) == []
    assert ledger_rows(world, "ope505_admitted") == []
    assert len(ledger_rows(world, "ope505_receipt")) == 1
    world.regrant("generated-starter-3", setup_policy_revision="generated-setup-rev-3")
    retry = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert retry == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}
    assert ledger_rows(world, "ope505_admitted") == []
    assert len(ledger_rows(world, RECEIPT_REFUSED)) == 1
    assert w.dispatch_pending()["admitted"] == []


@pytest.mark.parametrize("via", ["retry", "sweep"])
@pytest.mark.parametrize("case", ["withdrawn", "unavailable", "hold"])
def test_a2_a_submit_that_bound_nothing_does_not_bind_the_retry(
    tmp_path: Path, monkeypatch, case: str, via: str
) -> None:
    """Re-review A2: after a submit OPE-505 refused or held, a fresh grant simply submits."""
    from opentour_cell.pilot.intake_admission import DurableIntake
    from opentour_cell.pilot.intake_mcp import AdmissionHold
    from opentour_cell.pilot.intake_mcp import IntakeError as AdmissionError

    world = World(tmp_path / "w")
    selection = world.starter()
    w, sid = scanned(world)
    original = DurableIntake.submit_intake

    def bind_nothing(self, actor, request):  # type: ignore[no-untyped-def]
        if case == "withdrawn":
            grant_ref = world.grant_body["grant_ref"]
            _private(selection.parent / f"starter-intake-withdrawn-{grant_ref}.json", {})
            return original(self, actor, request)  # the real guard refuses: intake_unavailable
        if case == "unavailable":
            raise AdmissionError("intake_unavailable")
        return AdmissionHold("generated")

    monkeypatch.setattr(DurableIntake, "submit_intake", bind_nothing)
    held = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    reason = "OPE505_HOLD" if case == "hold" else "OPE505_UNAVAILABLE"
    assert held == {"state": "held", "reason": reason}
    assert world.ope505_rows("intake_runs") == 0 and world.ope505_rows("intake_keys") == 0
    monkeypatch.setattr(DurableIntake, "submit_intake", original)
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    if via == "retry":
        assert w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]["state"] == "admitted"
    else:
        swept = w.dispatch_pending()
        assert len(swept["admitted"]) == 1 and swept["held"] == []
    assert world.ope505_rows("intake_runs") == 1 and world.ope505_rows("intake_keys") == 1


@pytest.mark.parametrize("bound", [True, False])
def test_a2_an_unknown_outcome_is_adjudicated_by_ope505(
    tmp_path: Path, monkeypatch, bound: bool
) -> None:
    """An exception with an unknown outcome: OPE-505's own key binding decides the retry."""
    from opentour_cell.pilot.intake_admission import DurableIntake

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    original = DurableIntake.submit_intake

    def lost(self, actor, request):  # type: ignore[no-untyped-def]
        if bound:
            original(self, actor, request)  # committed, then the reply is lost
        raise OSError("generated lost reply")

    monkeypatch.setattr(DurableIntake, "submit_intake", lost)
    held = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert held == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
    monkeypatch.setattr(DurableIntake, "submit_intake", original)
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    retry = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    if bound:
        # the documented residual: a run whose receipt never reached the worker is not claimed
        assert retry == {"state": "held", "reason": "OPE505_INTENT_CHANGED"}
        assert world.ope505_rows("intake_runs") == 1
    else:
        assert retry["state"] == "admitted" and world.ope505_rows("intake_runs") == 1


def test_a6_the_sweep_names_an_adopted_draft_with_its_receipt_fields(
    tmp_path: Path, monkeypatch
) -> None:
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    held = held_on_record_change(monkeypatch, world, w, sid)
    receipt = ledger_rows(world, "ope505_receipt")[0][1]
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    assert w.dispatch_pending() == {
        "admitted": [held["draft_key"]],
        "reconciled": [{"draft_key": held["draft_key"], "run_ref": receipt["run_ref"]}],
        "held": [],
    }
    (adopted,) = [p for _, p in ledger_rows(world, "ope505_admitted")]
    assert adopted["reconciled"] is True and adopted["provenance"] == "ope505_receipt_at_submit"
    lineage = receipt["lineage"]
    assert adopted["benchmark_version"] == lineage["benchmark_version"] == 1
    assert adopted["benchmark_ref"] == lineage["benchmark_ref"]
    assert adopted["benchmark_ref"].startswith("benchmark-")
    assert adopted["workflow_ref"] == lineage["workflow_ref"]
    assert adopted["workflow_ref"].startswith("workflow-")
    assert adopted["ope505_parent_run"] is None and adopted["lineage_parent"] is None


def test_j6_concurrent_reconciles_of_one_draft_adopt_once(tmp_path: Path, monkeypatch) -> None:
    """The self-reconcile holds the store's write lock from its read to its adoption."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    held_on_record_change(monkeypatch, world, w, sid)
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    original_fault = entry.StarterBenchmarkSink._receipt_fault.__func__  # type: ignore[attr-defined]
    original_adopt = entry.StarterBenchmarkSink._adopt
    both, reached, adopted = threading.Event(), [0], []
    lock = threading.Lock()

    def fault(cls, db, draft_key, receipt, workflow, depth):  # type: ignore[no-untyped-def]
        result = original_fault(cls, db, draft_key, receipt, workflow, depth)
        if depth == 0:
            with lock:
                reached[0] += 1
                if reached[0] == 2:
                    both.set()
            # Serialized, the other reconcile cannot read until this one commits.
            both.wait(2.5)
        return result

    def adopt(self, db, draft_key, receipt, workflow):  # type: ignore[no-untyped-def]
        adopted.append(draft_key)
        return original_adopt(self, db, draft_key, receipt, workflow)

    monkeypatch.setattr(entry.StarterBenchmarkSink, "_receipt_fault", classmethod(fault))
    monkeypatch.setattr(entry.StarterBenchmarkSink, "_adopt", adopt)
    results = concurrently(world, sid, sid)
    assert len(adopted) == 1
    assert [r["ope505"]["state"] for r in results] == ["admitted", "admitted"]
    assert len(ledger_rows(world, "ope505_admitted")) == 1


def test_a_superseded_ancestors_receipt_still_stands_as_its_successors_parent(
    tmp_path: Path, monkeypatch
) -> None:
    """`DRAFT_SUPERSEDED` refuses adoption of the ancestor, never its receipt as a parent."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    finish = in_flight_at_get_run(monkeypatch, world, sid)
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    successor = w.call(OWNER, FREEZE, {"session_id": sid})
    assert successor["ope505"] == {
        "state": "held",
        "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT",
    }
    stale = finish()  # finishes after its successor exists: held, with its marker
    assert stale["ope505"] == {"state": "held", "reason": "DRAFT_SUPERSEDED"}
    assert [p["reason"] for _, p in ledger_rows(world, RECEIPT_REFUSED)] == ["DRAFT_SUPERSEDED"]
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    ope505 = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    r1 = ledger_rows(world, "ope505_receipt")[0][1]
    assert ope505["state"] == "admitted" and ope505["benchmark_version"] == 2
    assert ope505["run_ref"] != r1["run_ref"] and ope505["ope505_parent_run"] == r1["run_ref"]
    assert ope505["lineage_parent"]["state"] == "superseded_not_admitted"
    assert [p["draft_key"] for _, p in ledger_rows(world, "ope505_admitted")] == [
        successor["draft_key"]
    ]


def test_a_blocking_refusal_marker_alone_prevents_adoption(tmp_path: Path, monkeypatch) -> None:
    """Defence in depth: a lineage refusal stands even if the state behind it were lost."""
    world = World(tmp_path / "w")
    world.starter()
    _, full = scanned(world)
    _, narrow = scanned(world, deselect=["generated-n07"])
    gate_get_run(monkeypatch)
    results = concurrently(world, full, narrow)
    monkeypatch.undo()
    loser = next(r for r in results if r["ope505"]["state"] == "held")
    assert loser["ope505"]["reason"] == "OPE505_RUN_BOUND_TO_OTHER_DRAFT"
    assert [p["reason"] for _, p in ledger_rows(world, RECEIPT_REFUSED)] == [
        "OPE505_RUN_BOUND_TO_OTHER_DRAFT"
    ]
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:  # forge: drop the winner
        assert db.execute("DELETE FROM ledger WHERE kind='ope505_admitted'").rowcount == 1
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    retry = world.worker().call(OWNER, FREEZE, {"session_id": loser["session_id"]})["ope505"]
    assert retry == {"state": "held", "reason": "OPE505_RUN_BOUND_TO_OTHER_DRAFT"}
    assert ledger_rows(world, "ope505_admitted") == []


def test_a_draft_superseded_during_its_own_sweep_is_never_adopted(
    tmp_path: Path, monkeypatch
) -> None:
    """A5: the self-reconcile re-checks supersession under its write lock."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    held = held_on_record_change(monkeypatch, world, w, sid)
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    original = entry.StarterBenchmarkSink._open
    successors: list[Json] = []

    def concurrent_freeze(self):  # type: ignore[no-untyped-def]
        if not successors:  # another session freezes the fresh evidence meanwhile
            successors.append(entry.MixedIntakeService(w.store).freeze_starter(sid))
        return original(self)

    monkeypatch.setattr(entry.StarterBenchmarkSink, "_open", concurrent_freeze)
    swept = w.dispatch_pending()
    assert successors[0]["supersedes"] == held["draft_key"]
    assert swept["held"] == [{"draft_key": held["draft_key"], "reason": "DRAFT_SUPERSEDED"}]
    assert swept["admitted"] == [] and ledger_rows(world, "ope505_admitted") == []


def test_a2_an_admission_hold_frees_the_key_for_an_accurate_later_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """After an attempt that bound nothing, a later OPE-505 refusal is not an intent change."""
    from opentour_cell.pilot.intake_admission import DurableIntake
    from opentour_cell.pilot.intake_mcp import AdmissionHold

    world = World(tmp_path / "w")
    selection = world.starter()
    w, sid = scanned(world)
    original = DurableIntake.submit_intake
    monkeypatch.setattr(
        DurableIntake, "submit_intake", lambda self, actor, request: AdmissionHold("generated")
    )
    assert w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]["reason"] == "OPE505_HOLD"
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")

    def withdraw_then_submit(self, actor, request):  # type: ignore[no-untyped-def]
        _private(selection.parent / "starter-intake-withdrawn-generated-starter-2.json", {})
        return original(self, actor, request)

    monkeypatch.setattr(DurableIntake, "submit_intake", withdraw_then_submit)
    second = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert second == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
    monkeypatch.setattr(DurableIntake, "submit_intake", original)
    world.regrant("generated-starter-3", setup_policy_revision="generated-setup-rev-3")
    assert w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]["state"] == "admitted"
    assert world.ope505_rows("intake_runs") == 1


@pytest.mark.parametrize("regrant", [False, True])
def test_a_receipt_whose_lineage_was_never_read_is_never_adopted(
    tmp_path: Path, monkeypatch, regrant: bool
) -> None:
    """OPE-505 bound the run but its benchmark step failed: the lineage is unknown."""
    from opentour_cell.pilot.workflow_benchmarks import WorkflowBenchmarks

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    original = WorkflowBenchmarks.admit

    def fail_once(self, tenant, work):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(WorkflowBenchmarks, "admit", original)
        raise OSError("generated benchmark step failure")

    monkeypatch.setattr(WorkflowBenchmarks, "admit", fail_once)
    held = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert held == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
    assert ledger_rows(world, "ope505_receipt")[0][1]["lineage"] is None
    if not regrant:  # under the same grant, the retry resubmits and recovers
        assert w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]["state"] == "admitted"
        return
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    retry = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert retry == {"state": "held", "reason": "OPE505_LINEAGE_UNKNOWN"}
    assert ledger_rows(world, "ope505_admitted") == []


def _drop_journal(world: World, kind: str) -> None:
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        assert db.execute("DELETE FROM ledger WHERE kind=?", (kind,)).rowcount >= 1


def test_a_successor_after_a_pre_journal_admission_is_held_with_its_link(tmp_path: Path) -> None:
    """A store written before submits were journaled: the admitted ancestor alone holds it."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": sid})
    _drop_journal(world, "ope505_submit")
    _drop_journal(world, "ope505_receipt")
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    second = w.call(OWNER, FREEZE, {"session_id": sid})
    assert second["ope505"] == {
        "state": "held",
        "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT",
    }
    (hold,) = [p for _, p in ledger_rows(world, "dispatch_held")]
    assert hold["predecessor_draft_key"] == first["draft_key"]
    assert hold["predecessor_run_ref"] == first["ope505"]["run_ref"]
    assert world.ope505_rows("intake_runs") == 1


def test_a_successor_is_never_recorded_on_its_ancestors_journaled_run(
    tmp_path: Path, monkeypatch
) -> None:
    """Backstop when recording: even if the ancestor's submit row were lost."""
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    finish = in_flight_at_get_run(monkeypatch, world, sid)
    _drop_journal(world, "ope505_submit")  # forge: the pre-submit check cannot see it
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    fresh = w.call(OWNER, FREEZE, {"session_id": sid})
    stale = finish()
    assert fresh["ope505"] == {
        "state": "held",
        "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT",
    }
    assert stale["ope505"] == {"state": "held", "reason": "DRAFT_SUPERSEDED"}
    assert ledger_rows(world, "ope505_admitted") == []
    assert world.ope505_rows("intake_runs") == 1


def test_an_ancestor_submit_that_bound_nothing_never_blocks_its_successor(
    tmp_path: Path, monkeypatch
) -> None:
    from opentour_cell.pilot.intake_admission import DurableIntake
    from opentour_cell.pilot.intake_mcp import AdmissionHold

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    original = DurableIntake.submit_intake
    monkeypatch.setattr(
        DurableIntake, "submit_intake", lambda self, actor, request: AdmissionHold("generated")
    )
    first = w.call(OWNER, FREEZE, {"session_id": sid})
    assert first["ope505"] == {"state": "held", "reason": "OPE505_HOLD"}
    monkeypatch.setattr(DurableIntake, "submit_intake", original)
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    second = w.call(OWNER, FREEZE, {"session_id": sid})
    assert second["supersedes"] == first["draft_key"]
    ope505 = second["ope505"]
    assert ope505["state"] == "admitted" and ope505["benchmark_version"] == 1
    assert ope505["ope505_parent_run"] is None and ope505["lineage_parent"] is None
    assert world.ope505_rows("intake_runs") == 1


def test_k11_the_ancestors_check_and_submit_record_are_one_serialized_step(
    tmp_path: Path, monkeypatch
) -> None:
    """Review F1 (K11): a successor can never check between its ancestor's check and its
    submit record, so only one of the two ever reaches OPE-505 under one intent."""
    world = World(tmp_path / "w")
    w, sid = scanned(world)
    stale = w.call(OWNER, FREEZE, {"session_id": sid})  # frozen, not dispatched: no grant yet
    assert stale["ope505"]["state"] == "not_configured"
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.starter()
    original = entry.StarterBenchmarkSink._lineage
    ancestor_checked, successor_checked = threading.Event(), threading.Event()
    order: list[str] = []

    def lineage(db, draft_key, identity, workflow, link, run_ref=None):  # type: ignore[no-untyped-def]
        if run_ref is not None:
            return original(db, draft_key, identity, workflow, link, run_ref)
        order.append(draft_key)
        if draft_key == stale["draft_key"]:
            original(db, draft_key, identity, workflow, link, run_ref)
            ancestor_checked.set()
            # Serialized, the successor cannot even be frozen until this commits: times out.
            successor_checked.wait(2.0)
            return None
        try:
            return original(db, draft_key, identity, workflow, link, run_ref)
        finally:
            successor_checked.set()

    monkeypatch.setattr(entry.StarterBenchmarkSink, "_lineage", staticmethod(lineage))
    sweeper = world.worker()
    swept: list[Json] = []
    thread = threading.Thread(target=lambda: swept.append(sweeper.dispatch_pending()))
    thread.start()
    assert ancestor_checked.wait(30)
    fresh = w.call(OWNER, FREEZE, {"session_id": sid})
    thread.join(60)
    assert fresh["supersedes"] == stale["draft_key"]
    assert fresh["ope505"] == {
        "state": "held",
        "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT",
    }
    # The ancestor, serialized first, either recorded before its successor was frozen or was
    # superseded before recording; both are legitimate. What may never happen is two submits.
    assert swept[0]["admitted"] == [stale["draft_key"]] or swept[0]["held"] == [
        {"draft_key": stale["draft_key"], "reason": "DRAFT_SUPERSEDED"}
    ]
    submits = [p["draft_key"] for _, p in ledger_rows(world, "ope505_submit")]
    assert submits == [stale["draft_key"]]  # the successor never submitted
    assert world.ope505_rows("intake_keys") == 1 and world.ope505_rows("intake_runs") == 1


@pytest.mark.parametrize("proof", ["cause_free_refusal", "refusal_with_a_cause", "lost_reply"])
def test_f2_only_a_provably_unbound_ancestor_submit_frees_its_successor(
    tmp_path: Path, monkeypatch, proof: str
) -> None:
    """Review F2: OPE-505 refused the ancestor's submit; the successor may submit under the same
    grant only when the worker can prove OPE-505 bound nothing."""
    from opentour_cell.pilot.intake_admission import DurableIntake
    from opentour_cell.pilot.intake_mcp import IntakeError as AdmissionError

    world = World(tmp_path / "w")
    selection = world.starter()
    w, sid = scanned(world)
    original = DurableIntake.submit_intake
    withdrawn = selection.parent / "starter-intake-withdrawn-generated-starter.json"

    def refuse(self, actor, request):  # type: ignore[no-untyped-def]
        if proof == "cause_free_refusal":
            _private(withdrawn, {})  # OPE-505's own guard refuses before its transaction
            return original(self, actor, request)
        if proof == "refusal_with_a_cause":
            try:
                raise OSError("generated guard release failure")
            except OSError as error:  # the shape of a failure after OPE-505 could commit
                raise AdmissionError("intake_unavailable") from error
        raise OSError("generated lost reply")

    monkeypatch.setattr(DurableIntake, "submit_intake", refuse)
    first = w.call(OWNER, FREEZE, {"session_id": sid})
    assert first["ope505"] == {"state": "held", "reason": "OPE505_UNAVAILABLE"}
    assert world.ope505_rows("intake_runs") == 0 and world.ope505_rows("intake_keys") == 0
    outcome = [p["outcome"] for _, p in ledger_rows(world, "ope505_submit_outcome")]
    assert outcome == (
        ["bound_nothing"]
        if proof == "cause_free_refusal"
        else ["refused"]
        if proof == "refusal_with_a_cause"
        else []
    )
    monkeypatch.setattr(DurableIntake, "submit_intake", original)
    if withdrawn.exists():
        os.unlink(withdrawn)
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    second = w.call(OWNER, FREEZE, {"session_id": sid})
    assert second["supersedes"] == first["draft_key"]
    if proof == "cause_free_refusal":
        ope505 = second["ope505"]
        assert ope505["state"] == "admitted" and ope505["benchmark_version"] == 1
        assert ope505["lineage_parent"] is None and ope505["ope505_parent_run"] is None
        assert world.ope505_rows("intake_runs") == 1
    else:  # fail closed: OPE-505 may have bound the ancestor's key to this intent
        assert second["ope505"] == {
            "state": "held",
            "reason": "OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT",
        }
        assert world.ope505_rows("intake_runs") == 0


@pytest.mark.parametrize("field", ["benchmark_version", "benchmark_ref", "workflow_ref"])
def test_f3_a_record_time_read_that_disagrees_with_the_receipt_is_held(
    tmp_path: Path, monkeypatch, field: str
) -> None:
    """Review F3: the admitted row's OPE-505 fields must equal the submit-time receipt."""
    from dataclasses import replace

    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    original = BenchmarkIntake.get_run

    def drifted(self, actor, run_ref):  # type: ignore[no-untyped-def]
        status = original(self, actor, run_ref)
        if field == "workflow_ref":
            return replace(status, workflow_ref="workflow-generated-drift")
        attr = {"benchmark_version": "version", "benchmark_ref": "benchmark_ref"}[field]
        value = 99 if field == "benchmark_version" else "benchmark-generated-drift"
        return replace(status, benchmark=replace(status.benchmark, **{attr: value}))

    monkeypatch.setattr(BenchmarkIntake, "get_run", drifted)
    held = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert held == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}
    assert ledger_rows(world, "ope505_admitted") == []
    assert [p["reason"] for _, p in ledger_rows(world, RECEIPT_REFUSED)] == [
        "OPE505_LINEAGE_MISMATCH"
    ]
    monkeypatch.setattr(BenchmarkIntake, "get_run", original)
    # never adopted later either: the refusal is permanent for that receipt's run
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    retry = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert retry == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}


def test_f3_the_admitted_row_records_the_receipt_values(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    ope505 = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    lineage = ledger_rows(world, "ope505_receipt")[0][1]["lineage"]
    for key in ("benchmark_ref", "benchmark_version", "benchmark_state", "workflow_ref"):
        assert ope505[key] == lineage[key], key
    assert ope505["provenance"] == "ope505_receipt_confirmed_at_record"


def test_l3_a_lock_wait_past_the_busy_timeout_then_a_regrant_reconciles(
    tmp_path: Path, monkeypatch
) -> None:
    """Owner decision M1, repro L3: the recording transaction times out on a held lock."""
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    store = world.directory / entry.STORE_FILE
    real = sqlite3.connect
    holder: list[sqlite3.Connection] = []

    def short_timeout(path, *args, **kw):  # type: ignore[no-untyped-def]
        if str(path) == str(store) and not args:
            kw.setdefault("timeout", 0.2)
        return real(path, *args, **kw)

    original = BenchmarkIntake.get_run

    def lock_store(self, actor, run_ref):  # type: ignore[no-untyped-def]
        locker = real(store, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        holder.append(locker)
        return original(self, actor, run_ref)

    monkeypatch.setattr(sqlite3, "connect", short_timeout)
    monkeypatch.setattr(BenchmarkIntake, "get_run", lock_store)
    route = entry.ui_dispatch(w, OWNER)
    status, body = route("POST /v1/mixed-intake/freeze-starter", {"session_id": sid})
    assert (status, body) == (503, {"error": "WORKER_UNAVAILABLE"})
    holder[0].rollback()
    holder[0].close()
    monkeypatch.undo()
    assert ledger_rows(world, "ope505_admitted") == []
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    successor = w.call(OWNER, FREEZE, {"session_id": sid})
    admitted = [p for _, p in ledger_rows(world, "ope505_admitted")]
    r1 = ledger_rows(world, "ope505_receipt")[0][1]
    assert [p["draft_key"] for p in admitted] == [successor["draft_key"]]
    assert successor["ope505"]["benchmark_version"] == 2
    assert successor["ope505"]["ope505_parent_run"] == r1["run_ref"]
    assert successor["ope505"]["run_ref"] != r1["run_ref"]
    assert successor["ope505"]["lineage_parent"]["basis"] == "own_receipt"
    assert world.ope505_rows("intake_runs") == 2
    # the superseded ancestor is never adopted: its pending dispatch stays held
    assert w.dispatch_pending() == {
        "admitted": [],
        "reconciled": [],
        "held": [{"draft_key": r1["draft_key"], "reason": "DRAFT_SUPERSEDED"}],
    }


def test_an_ope505_idempotency_conflict_is_a_typed_intent_hold(tmp_path: Path, monkeypatch) -> None:
    from opentour_cell.pilot.intake_admission import DurableIntake
    from opentour_cell.pilot.intake_mcp import IntakeError as AdmissionError

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)

    def conflict(self, actor, request):  # type: ignore[no-untyped-def]
        raise AdmissionError("idempotency_conflict")

    monkeypatch.setattr(DurableIntake, "submit_intake", conflict)
    held = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    assert held == {"state": "held", "reason": "OPE505_INTENT_CHANGED"}


def test_adoption_never_takes_a_run_another_draft_holds(tmp_path: Path) -> None:
    """Even with its refusal marker gone (a forged store), a bound run is never adopted."""
    world = World(tmp_path / "w")
    world.starter()
    w, full = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": full})
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        (row_id, payload) = db.execute(
            "SELECT id, payload FROM ledger WHERE kind='ope505_admitted'"
        ).fetchone()
        legacy = {
            k: v
            for k, v in json.loads(payload).items()
            if k not in ("run_identity", "workflow_identity")
        }
        db.execute("UPDATE ledger SET payload=? WHERE id=?", (json.dumps(legacy), row_id))
    w, narrow = scanned(world, deselect=["generated-n07"])
    loser = w.call(OWNER, FREEZE, {"session_id": narrow})
    assert loser["ope505"] == {"state": "held", "reason": "OPE505_RUN_BOUND_TO_OTHER_DRAFT"}
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        assert db.execute("DELETE FROM ledger WHERE kind='ope505_receipt_refused'").rowcount == 1
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": narrow, "incremental": True})
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    successor = w.call(OWNER, FREEZE, {"session_id": narrow})
    assert successor["supersedes"] == loser["draft_key"]
    # not adopted: the successor is judged without it, and OPE-505's parent is not claimable
    assert successor["ope505"] == {"state": "held", "reason": "OPE505_LINEAGE_MISMATCH"}
    assert [p["draft_key"] for _, p in ledger_rows(world, "ope505_admitted")] == [
        first["draft_key"]
    ]


def receipt_gate(monkeypatch, thread_of_interest: list[threading.Thread]):  # type: ignore[no-untyped-def]
    """Hold the chosen thread just before it journals its OPE-505 receipt."""
    from opentour_cell.pilot.mixed_intake.store import IntakeStore

    reached, release, logged = threading.Event(), threading.Event(), threading.Event()
    original = IntakeStore.log

    def log(db, session_id, kind, payload):  # type: ignore[no-untyped-def]
        mine = thread_of_interest and threading.current_thread() is thread_of_interest[0]
        if mine and kind == entry.RECEIPT:
            reached.set()
            assert release.wait(30)
            original(db, session_id, kind, payload)
            logged.set()
            return
        original(db, session_id, kind, payload)

    monkeypatch.setattr(IntakeStore, "log", staticmethod(log))
    return reached, release, logged


def test_a13_a_receipt_journaled_during_a_successors_dispatch_verifies_its_parent(
    tmp_path: Path, monkeypatch
) -> None:
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    stale_thread: list[threading.Thread] = []
    reached, release, logged = receipt_gate(monkeypatch, stale_thread)
    original = BenchmarkIntake.get_run

    def successor_waits(self, actor, run_ref):  # type: ignore[no-untyped-def]
        if threading.current_thread() is threading.main_thread():
            release.set()  # the stale receipt lands after the successor's pre-submit checks
            assert logged.wait(30)
        return original(self, actor, run_ref)

    monkeypatch.setattr(BenchmarkIntake, "get_run", successor_waits)
    thread = threading.Thread(
        target=lambda: world.worker().call(OWNER, FREEZE, {"session_id": sid})
    )
    stale_thread.append(thread)
    thread.start()
    assert reached.wait(30)
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    successor = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    thread.join(60)
    assert successor["state"] == "admitted" and successor["benchmark_version"] == 2
    admitted = [p for _, p in ledger_rows(world, "ope505_admitted")]
    r1 = next(
        p
        for _, p in ledger_rows(world, "ope505_receipt")
        if p["draft_key"] != successor["draft_key"]
    )
    assert [p["draft_key"] for p in admitted] == [successor["draft_key"]]
    # the receipt journaled after the successor's pre-submit checks is read when recording
    assert successor["ope505_parent_run"] == r1["run_ref"] != successor["run_ref"]
    assert successor["lineage_parent"]["draft_key"] == r1["draft_key"]
    assert world.ope505_rows("intake_runs") == 2


def test_a14_two_held_ancestors_verify_as_a_receipt_chain(tmp_path: Path, monkeypatch) -> None:
    from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    saved = json.loads(json.dumps(world.record))
    stale_thread: list[threading.Thread] = []
    reached, release, logged = receipt_gate(monkeypatch, stale_thread)
    original = BenchmarkIntake.get_run

    def change_record_for_main(self, actor, run_ref):  # type: ignore[no-untyped-def]
        if threading.current_thread() is threading.main_thread():
            world.record["connections"][LOCAL]["page_size"] = 7
            world.save()
        return original(self, actor, run_ref)

    monkeypatch.setattr(BenchmarkIntake, "get_run", change_record_for_main)
    thread = threading.Thread(
        target=lambda: world.worker().call(OWNER, FREEZE, {"session_id": sid})
    )
    stale_thread.append(thread)
    thread.start()
    assert reached.wait(30)  # D1 reached OPE-505; its receipt is not journaled yet
    edit_n05(world, "generated-rev-05-2")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-2", setup_policy_revision="generated-setup-rev-2")
    second = w.call(OWNER, FREEZE, {"session_id": sid})  # D2 becomes R2, then the record moves
    assert second["ope505"] == {"state": "held", "reason": "WORKER_RECORD_CHANGED"}
    world.record = json.loads(json.dumps(saved))
    world.save()
    release.set()
    thread.join(60)
    assert ledger_rows(world, "ope505_admitted") == []
    monkeypatch.setattr(BenchmarkIntake, "get_run", original)
    edit_n05(world, "generated-rev-05-3")
    w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
    world.regrant("generated-starter-3", setup_policy_revision="generated-setup-rev-3")
    third = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
    admitted = [p for _, p in ledger_rows(world, "ope505_admitted")]
    receipts = {p["draft_key"]: p for _, p in ledger_rows(world, "ope505_receipt")}
    assert [p["draft_key"] for p in admitted] == [third["draft_key"]]
    assert third["state"] == "admitted" and third["benchmark_version"] == 3
    # v3's parent is v2's run, whose own parent (v1's run) verified through the chain
    parent = third["lineage_parent"]
    assert parent["basis"] == "own_receipt" and parent["draft_key"] == second["draft_key"]
    assert third["ope505_parent_run"] == receipts[second["draft_key"]]["run_ref"]
    assert third["run_ref"] not in {p["run_ref"] for p in receipts.values() if p is not None} - {
        receipts[third["draft_key"]]["run_ref"]
    }
    first_run = next(p["run_ref"] for k, p in receipts.items() if k != second["draft_key"])
    assert receipts[second["draft_key"]]["lineage"]["parent_run"] == first_run
    assert world.ope505_rows("intake_runs") == 3


def test_k21_a_v3_successor_links_to_the_last_admitted_version(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    runs = [w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]["run_ref"]]
    for n, revision in ((2, "generated-rev-05-2"), (3, "generated-rev-05-3")):
        edit_n05(world, revision)
        w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": True})
        world.regrant(f"generated-starter-{n}", setup_policy_revision=f"generated-setup-rev-{n}")
        ope505 = w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]
        assert ope505["state"] == "admitted" and ope505["benchmark_version"] == n
        assert ope505["ope505_parent_run"] == runs[-1]
        runs.append(ope505["run_ref"])
    assert world.ope505_rows("intake_runs") == 3 and len(set(runs)) == 3


def test_k20_the_ope505_workflow_boundary_is_the_grants_creation_ref(tmp_path: Path) -> None:
    """An unrelated draft is held in another draft's workflow, never in a new workflow."""
    world = World(tmp_path / "w")
    world.starter()
    w, full = scanned(world)
    w.call(OWNER, FREEZE, {"session_id": full})
    w, narrow = scanned(world, deselect=["generated-n07"])
    held = w.call(OWNER, FREEZE, {"session_id": narrow})["ope505"]
    assert held == {"state": "held", "reason": "OPE505_RUN_BOUND_TO_OTHER_DRAFT"}
    world.regrant(
        "generated-starter-2",
        setup_policy_revision="generated-setup-rev-2",
        creation_ref="generated-creation-2",
    )
    fresh = w.call(OWNER, FREEZE, {"session_id": narrow})["ope505"]
    assert fresh["state"] == "admitted" and fresh["benchmark_version"] == 1
    assert fresh["ope505_parent_run"] is None and fresh["lineage_parent_draft_key"] is None
    assert world.ope505_rows("intake_workflows") == 2


@pytest.mark.parametrize("clash", ["draft_key", "run_ref"])
def test_l1_a_store_with_a_duplicate_admission_is_refused_without_a_partial_index(
    tmp_path: Path, clash: str
) -> None:
    world = World(tmp_path / "w")
    store = entry.IntakeStore(world.directory / entry.STORE_FILE)  # no worker yet: no indexes
    rows = [
        {"draft_key": "generated-draft-a", "run_ref": "run-generated-a"},
        {"draft_key": "generated-draft-b", "run_ref": "run-generated-b"},
    ]
    rows[1][clash] = rows[0][clash]
    with store.tx() as db:
        for row in rows:
            store.log(db, "generated-session", "ope505_admitted", row)
    with pytest.raises(entry.WorkerRefused) as error:
        world.worker()
    assert error.value.code == "WORKER_STORE_INCONSISTENT"
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        names = {
            name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert not names & {"ope505_admitted_draft", "ope505_admitted_run"}


def test_n6_the_sweep_continues_past_every_hold(tmp_path: Path) -> None:
    """Re-review N6: holds before an admissible draft never stop the sweep."""
    world = World(tmp_path / "w")
    w, narrow = scanned(world, deselect=["generated-n01"])
    first_held = w.call(OWNER, FREEZE, {"session_id": narrow})["draft_key"]
    w, remote = scanned(world, REMOTE)
    second_held = w.call(OWNER, FREEZE, {"session_id": remote})["draft_key"]
    w, full = scanned(world)
    admissible = w.call(OWNER, FREEZE, {"session_id": full})["draft_key"]
    del world.record["connections"][REMOTE]
    world.starter()
    assert w.dispatch_pending() == {
        "admitted": [admissible],
        "reconciled": [],
        "held": [
            {"draft_key": first_held, "reason": NOT_COVERED},
            {"draft_key": second_held, "reason": "CONNECTION_NOT_GRANTED"},
        ],
    }


def test_n16_a_freeze_never_sweeps_other_pending_drafts(tmp_path: Path) -> None:
    """Re-review N16: another session's admissible pending draft is not dispatched first."""
    world = World(tmp_path / "w")
    w, other = scanned(world, deselect=["generated-n07"])
    parked = w.call(OWNER, FREEZE, {"session_id": other})["draft_key"]
    world.starter()
    w, own = scanned(world)
    frozen = w.call(OWNER, FREEZE, {"session_id": own})
    assert frozen["ope505"]["state"] == "admitted"
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        assert db.execute(
            "SELECT dispatched FROM drafts WHERE draft_key=?", (parked,)
        ).fetchone() == (0,)
    assert [p["draft_key"] for _, p in ledger_rows(world, "ope505_admitted")] == [
        frozen["draft_key"]
    ]
    assert ledger_rows(world, "dispatch_held") == []


def test_a_tampered_session_tenant_is_held_before_the_sink_is_reached(tmp_path: Path) -> None:
    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        db.execute("UPDATE sessions SET tenant=? WHERE session_id=?", ("generated-tenant-b", sid))
    held = w.call(OWNER, FREEZE, {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "GRANT_CHANGED"}
    assert world.ope505_rows("intake_runs") == 0


def test_a_run_ope505_returns_for_another_draft_is_never_claimed(tmp_path: Path) -> None:
    """Defense in depth: an admission entry without a run identity (as 1628445 wrote them)."""
    world = World(tmp_path / "w")
    world.starter()
    w, full = scanned(world)
    first = w.call(OWNER, FREEZE, {"session_id": full})
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        (row_id, payload) = db.execute(
            "SELECT id, payload FROM ledger WHERE kind='ope505_admitted'"
        ).fetchone()
        legacy = {
            k: v
            for k, v in json.loads(payload).items()
            if k not in ("run_identity", "workflow_identity")
        }
        db.execute("UPDATE ledger SET payload=? WHERE id=?", (json.dumps(legacy), row_id))
    w, narrow = scanned(world, deselect=["generated-n07"])
    other = w.call(OWNER, FREEZE, {"session_id": narrow})
    assert other["ope505"] == {"state": "held", "reason": "OPE505_RUN_BOUND_TO_OTHER_DRAFT"}
    assert [p["draft_key"] for _, p in ledger_rows(world, "ope505_admitted")] == [
        first["draft_key"]
    ]
    assert world.ope505_rows("intake_runs") == 1


def test_an_ope505_admission_hold_is_reported_as_a_typed_hold(tmp_path: Path, monkeypatch) -> None:
    """Review M13: an `AdmissionHold` from OPE-505 is a typed hold, never an admission."""
    from opentour_cell.pilot.intake_admission import DurableIntake
    from opentour_cell.pilot.intake_mcp import AdmissionHold

    world = World(tmp_path / "w")
    world.starter()
    w, sid = scanned(world)
    monkeypatch.setattr(
        DurableIntake, "submit_intake", lambda self, actor, request: AdmissionHold("generated")
    )
    held = w.call(OWNER, FREEZE, {"session_id": sid})
    assert held["ope505"] == {"state": "held", "reason": "OPE505_HOLD"}
    assert ledger_rows(world, "ope505_admitted") == []
    monkeypatch.undo()
    assert w.call(OWNER, FREEZE, {"session_id": sid})["ope505"]["state"] == "admitted"


# ------------------------------------------------------------------ argument and record bounds


def test_record_level_deselection_cannot_be_widened_by_the_caller(tmp_path: Path) -> None:
    """Review M18: the owner's record-level deselection always applies."""
    world = World(tmp_path / "w")
    world.record["connections"][LOCAL]["deselected"] = ["generated-n07"]
    world.save()
    w, sid = scanned(world)
    items = w.call(OWNER, "mixed_intake_inventory", {"session_id": sid})["items"]
    assert next(i for i in items if i["ref"] == "generated-n07")["state"] == "deselected"
    w2, sid2 = scanned(world, deselect=["generated-n06"])
    items = w2.call(OWNER, "mixed_intake_inventory", {"session_id": sid2})["items"]
    states = {i["ref"]: i["state"] for i in items}
    assert states["generated-n07"] == states["generated-n06"] == "deselected"
    # an empty caller deselection is still narrowed by the record
    w3, sid3 = scanned(world, deselect=[])
    assert sid3 == sid


def test_the_worker_directory_must_be_private_to_this_uid(tmp_path: Path, monkeypatch) -> None:
    """Review M23: mode, owner, kind and absoluteness of the worker directory are enforced."""
    world = World(tmp_path / "w")
    real = os.getuid()

    def code(directory: Path) -> str:
        with pytest.raises(entry.WorkerRefused) as error:
            entry.MixedIntakeWorker(directory)
        return error.value.code

    assert code(Path(os.path.relpath(world.directory))) == "PRIVATE_DIRECTORY_REQUIRED"
    for mode in (0o750, 0o705, 0o701, 0o710):
        world.directory.chmod(mode)
        assert code(world.directory) == "PRIVATE_DIRECTORY_REQUIRED"
    world.directory.chmod(0o700)
    link = world.base / "link"
    link.symlink_to(world.directory)
    assert code(link) == "PRIVATE_DIRECTORY_REQUIRED"
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    assert code(world.directory) == "PRIVATE_DIRECTORY_REQUIRED"
    monkeypatch.undo()
    assert entry.MixedIntakeWorker(world.directory).directory == world.directory


def test_trial_max_items_is_bounded(tmp_path: Path) -> None:
    """Review M19."""
    world = World(tmp_path / "w")
    w = world.worker()
    scope = {"connection_ref": LOCAL, "purpose": "policy"}
    for bad in (0, 6, -1, True, False, 1.0, "1", None):
        assert refused(w, OWNER, "mixed_intake_trial", {**scope, "max_items": bad}) == (
            "INVALID_ARGUMENTS"
        ), bad
    for good in (1, 5):
        assert w.call(OWNER, "mixed_intake_trial", {**scope, "max_items": good})["operation"] == (
            "mixed_intake_trial"
        )


def test_citation_locator_is_bounded_in_size_and_shape(tmp_path: Path) -> None:
    """Review M16."""
    world = World(tmp_path / "w")
    w, sid = scanned(world)
    base = {"session_id": sid, "native_id": "generated-n01", "revision": "generated-rev-01-1"}
    ok = w.call(
        OWNER,
        "mixed_intake_citation",
        {**base, "locator": {"kind": "lines", "start": 3, "end": 4}},
    )
    assert ok["operation"] == "mixed_intake_citation"
    sixteen = {f"k{i:02d}": i for i in range(16)}
    assert refused(w, OWNER, "mixed_intake_citation", {**base, "locator": sixteen}) == (
        "LOCATOR_NOT_FOUND"  # within bounds: refused by the service, not by the worker
    )
    big = {"kind": "lines", "pad": "x" * 1100}
    for bad in (
        big,
        {**sixteen, "k16": 16},
        {},
        [],
        "lines",
        {1: 2},
        {"kind": float("nan")},
        {"kind": object()},
    ):
        assert refused(w, OWNER, "mixed_intake_citation", {**base, "locator": bad}) == (
            "INVALID_ARGUMENTS"
        ), bad
    edge = {"kind": "lines", "pad": "x" * (1024 - len(json.dumps({"kind": "lines", "pad": ""})))}
    assert len(json.dumps(edge, sort_keys=True)) == 1024
    assert refused(w, OWNER, "mixed_intake_citation", {**base, "locator": edge}) == (
        "LOCATOR_NOT_FOUND"
    )


def test_run_options_are_exactly_typed(tmp_path: Path) -> None:
    """Review M21: `incremental` is a bool and `max_pages` a bounded int, never truthy values."""
    world = World(tmp_path / "w")
    w = world.worker()
    sid = w.call(OWNER, "mixed_intake_open", {"connection_ref": LOCAL, "purpose": "policy"})[
        "session_id"
    ]
    for bad in (1, 0, "true", None, [], {}):
        assert refused(w, OWNER, "mixed_intake_run", {"session_id": sid, "incremental": bad}) == (
            "INVALID_ARGUMENTS"
        ), bad
    for bad in (0, 10_001, True, "1", 1.5):
        assert refused(w, OWNER, "mixed_intake_run", {"session_id": sid, "max_pages": bad}) == (
            "INVALID_ARGUMENTS"
        ), bad
    with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
        assert db.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 0
    assert w.call(OWNER, "mixed_intake_run", {"session_id": sid, "incremental": False})[
        "status"
    ] == ("sealed")


def test_hostile_operations_and_arguments_are_refused_by_name_only(tmp_path: Path) -> None:
    """Review F8 and the allowlist: an unhashable or lookalike operation is an unknown one."""
    world = World(tmp_path / "w")
    w = world.worker()

    class Lookalike(str):
        pass

    for op in (["mixed_intake_status"], {"a": 1}, None, 3, Lookalike("mixed_intake_status")):
        assert refused(w, OWNER, op, {"session_id": "generated-x"}) == "UNKNOWN_OPERATION", op
    for name in ("intake.status", "withdraw_grant", "dispatch_pending", "__init__"):
        assert refused(w, OWNER, name, {}) == "UNKNOWN_OPERATION"
    scope = {"connection_ref": LOCAL, "purpose": "policy"}
    # the caller never names the actor, the tenant or any authority field in its arguments
    for extra in ({"actor": OWNER}, {"actor": OBSERVER}, {"role": "owner"}, {"session": "x"}):
        assert refused(w, OWNER, "mixed_intake_preview", {**scope, **extra}) == (
            "INVALID_ARGUMENTS"
        )
    for bad in (None, [], "x", [("connection_ref", LOCAL)]):
        assert refused(w, OWNER, "mixed_intake_preview", bad) == "INVALID_ARGUMENTS"
    call = entry.mcp_dispatch(w, OWNER)
    assert call(["mixed_intake_status"], {}) == {  # type: ignore[arg-type]
        "isError": True,
        "content": [{"type": "text", "text": "UNKNOWN_OPERATION"}],
    }


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


RANDOM_REFS = (
    "run_ref",
    "correlation_ref",
    "workflow_ref",
    "benchmark_ref",
    "predecessor_run_ref",
    "ope505_parent_run",
)


def _normalized(value: object) -> object:
    """OPE-505 mints random run, workflow and benchmark refs; compare everything else exactly."""
    if isinstance(value, dict):
        return {
            k: ("<ref>" if k in RANDOM_REFS and v is not None else _normalized(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_normalized(v) for v in value]
    return value


@pytest.mark.parametrize("granted", [False, True])
def test_stdio_subprocess_and_ui_route_agree_on_mutating_operations(
    tmp_path: Path, granted: bool
) -> None:
    """Review F7 and re-review G5: the stdio process and the UI route agree, OPE-505 included."""
    by_stdio, by_route = World(tmp_path / "a"), World(tmp_path / "b")
    if granted:
        by_stdio.starter()
        by_route.starter()
    scope = {"connection_ref": LOCAL, "purpose": "policy", "deselect": ["generated-n07"]}
    opened = entry.ui_dispatch(by_route.worker(), OWNER)("POST /v1/mixed-intake/open", scope)
    assert opened[0] == 200
    sid = opened[1]["session_id"]
    sequence = [
        ("mixed_intake_open", scope),
        ("mixed_intake_run", {"session_id": sid, "max_pages": 2}),
        ("mixed_intake_run", {"session_id": sid}),
        ("mixed_intake_freeze_starter", {"session_id": sid}),
        ("mixed_intake_freeze_starter", {"session_id": sid}),
        ("mixed_intake_triage_packet", {"session_id": sid}),
        ("mixed_intake_run", {"session_id": sid, "incremental": "yes"}),
    ]
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
    ]
    for index, (name, arguments) in enumerate(sequence, start=2):
        frames.append(
            {"id": index, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
    stdin = "".join(json.dumps({"jsonrpc": "2.0", **f}) + "\n" for f in frames)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.mixed_intake.worker",
            "--directory",
            str(by_stdio.directory),
            "--actor",
            OWNER,
        ],
        input=stdin,
        text=True,
        capture_output=True,
        timeout=120,
        check=True,
    )
    replies = {r["id"]: r for r in map(json.loads, done.stdout.splitlines())}
    route = entry.ui_dispatch(by_route.worker(), OWNER)
    routes = {v: k for k, v in entry.UI_ROUTES.items()}
    for index, (name, arguments) in enumerate(sequence, start=2):
        status, body = route(routes[name], arguments)
        result = replies[index]["result"]
        if status == 200:
            assert result.get("isError") is not True
            assert _normalized(result["structuredContent"]) == _normalized(body)
        else:
            assert result["isError"] is True
            assert result["content"] == [{"type": "text", "text": body["error"]}]
    assert replies[8]["result"]["content"][0]["text"] == "INVALID_ARGUMENTS"
    frozen = replies[5]["result"]["structuredContent"]
    assert frozen["created"] is True
    assert frozen["ope505"]["state"] == ("admitted" if granted else "not_configured")
    assert replies[6]["result"]["structuredContent"]["ope505"] == frozen["ope505"]
    # the UI route on the stdio process's own directory reads the identical admission
    status, body = entry.ui_dispatch(by_stdio.worker(), OWNER)(
        "POST /v1/mixed-intake/freeze-starter", {"session_id": sid}
    )
    assert status == 200 and body["ope505"] == frozen["ope505"]
    if granted:
        for world in (by_stdio, by_route):
            assert world.ope505_rows("intake_runs") == world.ope505_rows("benchmarks") == 1
            assert len(ledger_rows(world, "ope505_admitted")) == 1
    # both stores hold the same durable outcome
    outcome = []
    for world in (by_stdio, by_route):
        with sqlite3.connect(world.directory / entry.STORE_FILE) as db:
            outcome.append(
                (
                    db.execute("SELECT draft_key, dispatched FROM drafts").fetchall(),
                    db.execute("SELECT kind FROM ledger ORDER BY id").fetchall(),
                    db.execute("SELECT status, fingerprint FROM sessions").fetchall(),
                )
            )
    assert outcome[0] == outcome[1]
