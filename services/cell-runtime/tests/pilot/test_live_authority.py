"""Actual signed authority and durable resolver through the scoped intake transport."""

import hmac
import io
import json
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from opentour_cell.pilot.engagement_epochs import EngagementLedger
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import SELECTION_VERSION, open_intake
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, parse_intake, serve_intake
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.live_authority import (
    SCHEMA,
    SELECTION_SCHEMA,
    LocalLiveIntake,
    PrivateLiveAuthority,
)
from opentour_cell.pilot.live_evidence import LiveEvidence, _canonical, binding_candidate
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.training import LocalTrainingStore
from test_intake_mcp import STARTER, _mandate, _starter_only, _write_private
from test_live_evidence import DECISION, OUTCOME

KEY = bytes.fromhex("6a" * 32)


def _historical_selection(path):
    path.mkdir(mode=0o700)
    mandate = _mandate(path, "org-a")
    epoch = LocalTrainingStore(path / "training.sqlite3", "org-a", existing_only=True).engagement_id
    scope = mandate["scope"]
    source = {
        k: scope[k] for k in ("workflow", "trace_source", "correction_source", "field", "fields")
    }
    source.update(
        source_revision="d" * 64,
        trace_sha256="a" * 64,
        correction_sha256="b" * 64,
        rows=2,
        connector=scope["connectors"][0],
    )
    authorized = LearningMandates(path, "org-a", epoch).authorize_source(
        mandate["mandate_id"], source, actor="worker"
    )
    template = Path(__file__).resolve().parents[2] / "examples/pilot-source-grant.template.json"
    grant = json.loads(template.read_text())
    grant.update(
        {
            k: source[k]
            for k in (
                "workflow",
                "field",
                "trace_source",
                "correction_source",
                "trace_sha256",
                "correction_sha256",
            )
        },
        organization="org-a",
        engagement=epoch,
        owner_id=scope["owner_id"],
        expires_at=scope["expires_at"],
        permitted_features=scope["fields"],
        delegation={
            "authority_kind": authorized["authority_kind"],
            "run_id": authorized["run_id"],
            "delegate": "worker",
        },
    )
    _write_private(path / ("cycle-source-" + authorized["run_id"] + ".json"), grant)
    request = {
        "pair_ref": "fixture-pair",
        "source_revision": source["trace_sha256"],
        "correction_revision": source["correction_sha256"],
        "policy_revision": scope["agreement_revision"],
    }
    selection = {
        **request,
        "schema_version": SELECTION_VERSION,
        "tenant": "org-a",
        "actor": "worker",
        "engagement_epoch": epoch,
        "mandate_id": mandate["mandate_id"],
        "mandate_revision": mandate["mandate_revision"],
        "source_run_id": authorized["run_id"],
        "workflow_ref": scope["workflow"],
        "trace_source": scope["trace_source"],
        "correction_source": scope["correction_source"],
    }
    filename = path / "historical-selection.json"
    _write_private(filename, selection)
    return filename, selection, {**request, "idempotency_key": "admit"}


def enroll_ledger(directory: Path, tenant: str, epoch: str, grants) -> EngagementLedger:
    """Generated sentinel enrollment: every grant now requires an enrolled, active ledger."""
    ledger = EngagementLedger(directory)
    ledger.record_engagement(
        tenant,
        epoch,
        lease_ref=grants[0].lease_ref,
        authority_digest="a" * 64,
        resources=tuple(g.resource for g in grants),
        lineage_ref="generated-trusted-cut",
        checkpoint=min(g.checkpoint for g in grants),
        budget_limit=100,
    )
    ledger.activate(tenant, epoch)
    return ledger


class SignedCase:
    def __init__(self, path: Path, *, historical=False, enroll=True):
        if historical:
            selection_path, selection, request_body = _historical_selection(path)
        else:
            selection_path, selection = _starter_only(path)
            request_body = STARTER
        self.actor, admission = open_intake(selection_path)
        admitted = admission.submit_intake(self.actor, parse_intake(request_body))
        self.workflow = admission.get_run(self.actor, admitted.run_ref).workflow_ref
        self.directory = path
        self.body = {
            "schema_version": SCHEMA,
            "tenant": "org-a",
            "workflow_ref": self.workflow,
            "engagement_epoch": selection["engagement_epoch"],
            "dataset_ref": "dataset-a",
            "dataset_revision": "dataset-v1",
            "custodian": "data-custodian",
            "expires_at": int(time.time()) + 3600,
            "state": "current",
            "actors": {
                "worker": ["intake"],
                "data-custodian": ["inspect", "custodian"],
                "owner": ["inspect"],
            },
            "grants": [asdict(DECISION), asdict(OUTCOME)],
            # Mandatory: a grant without this field is refused (see the tests at the end).
            "engagement_ledger_required": True,
        }
        _write_private(path / "live-authority-key.json", {"key": KEY.hex()})
        self.write()
        if enroll:
            # "other-decisions" is a second decision resource that some tests add to the grant
            # later (test_local_decision_context); the ledger must already cover it.
            extra = replace(DECISION, resource="other-decisions")
            self.ledger = enroll_ledger(
                path, "org-a", selection["engagement_epoch"], (DECISION, OUTCOME, extra)
            )
        self.authority = PrivateLiveAuthority(path, "org-a", self.workflow)
        self.live = LiveEvidence(path, self.authority)
        self.custodian = IntakePrincipal("org-a", "data-custodian", "inspect")
        self.owner = IntakePrincipal("org-a", "owner", "inspect")

    def write(self):
        _write_private(
            self.directory / f"live-authority-{self.workflow}.json",
            {
                "grant": self.body,
                "seal_sha256": hmac.digest(KEY, _canonical(self.body).encode(), "sha256").hex(),
            },
        )

    def connect(self, resource="decisions"):
        with self.authority.guard(self.custodian, self.workflow) as scope:
            assert scope.can_bind
            self.live.initialize()
            return self.live.save_binding(self.custodian, self.workflow, resource)

    def intake(self):
        authority = LocalLiveIntake(self.authority, self.actor.actor)
        return DurableIntake(self.directory, authority)


def request(reference, **changes):
    return parse_intake(
        {
            "mode": "live",
            "feed_binding_ref": reference,
            "source_revision": "source-v1",
            "idempotency_key": "live",
            **changes,
        }
    )


def test_historical_admission_can_connect_under_distinct_current_live_grant(tmp_path):
    case = SignedCase(tmp_path / "historical", historical=True)
    ledger = case.directory / "workflow-benchmarks.sqlite3"
    before = ledger.read_bytes()
    with sqlite3.connect(case.directory / "intake.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM intake_workflows").fetchone()[0] == 0
    reference = case.connect()
    assert case.connect() == reference
    assert case.live.resolve(reference, case.actor).workflow_ref == case.workflow
    receipt = case.intake().submit_intake(case.actor, request(reference))
    assert case.intake().get_run(case.actor, receipt.run_ref).workflow_ref == case.workflow
    assert ledger.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["tenant", "epoch", "absent", "orphan", "signed_epoch", "unsigned_actor"]
)
def test_neighboring_historical_identity_never_creates_live_state(tmp_path, fault):
    case = SignedCase(tmp_path / "historical", historical=True)
    ledger = case.directory / "workflow-benchmarks.sqlite3"
    if fault in {"tenant", "epoch"}:
        with sqlite3.connect(ledger) as db:
            if fault == "tenant":
                db.execute("UPDATE benchmarks SET tenant='foreign'")
            else:
                db.execute("UPDATE benchmarks SET epoch='previous'")
    if fault == "absent":
        ledger.unlink()
    if fault == "orphan":
        with sqlite3.connect(case.directory / "intake.sqlite3") as db:
            db.execute("DELETE FROM intake_benchmark_admissions")
    if fault == "signed_epoch":
        case.body["engagement_epoch"] = "previous"
        case.write()
    if fault == "unsigned_actor":
        case.body["actors"].pop(case.custodian.actor)
        case.body["custodian"] = "owner"
        case.body["actors"]["owner"] = ["custodian"]
        case.write()
    before = {p.name: p.read_bytes() for p in case.directory.iterdir() if p.is_file()}
    with pytest.raises(IntakeError):
        case.connect()
    assert before == {p.name: p.read_bytes() for p in case.directory.iterdir() if p.is_file()}


def test_actual_binding_store_vectors_and_restart(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    assert case.live.current_bindings(case.owner, case.workflow) == ()
    assert not case.live.path.exists()
    reference = case.connect()
    assert case.connect() == reference
    original = case.intake().submit_intake(case.actor, request(reference))
    assert (
        case.intake().submit_intake(case.actor, request(reference, idempotency_key="retry"))
        == original
    )
    current = case.intake().get_run(case.actor, original.run_ref)
    assert (
        current.workflow_ref == case.workflow and current.reason_code == "live_reader_unavailable"
    )
    with case.live._db() as db:
        assert db.execute("SELECT count(*) FROM live_events").fetchone()[0] == 0
    case.body["grants"][0]["revision"] = "grant-v2"
    case.write()
    successor = case.connect()
    held = case.intake().submit_intake(case.actor, request(reference))
    assert held.reason_code == "binding_superseded" and held.successor_ref == successor
    case.body["state"] = "revoked"
    case.write()
    with pytest.raises(IntakeError):
        case.intake().submit_intake(case.actor, request(reference))
    assert case.live.resolve(successor, case.actor) is None


def test_normal_intake_factory_uses_explicit_live_selection(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    reference = case.connect()
    selected = case.directory / "selected-live.json"
    _write_private(
        selected,
        {
            "schema_version": SELECTION_SCHEMA,
            "tenant": "org-a",
            "workflow_ref": case.workflow,
            "actor": "worker",
        },
    )
    actor, intake = open_intake(selected)
    receipt = intake.submit_intake(actor, request(reference))
    assert intake.get_run(actor, receipt.run_ref).workflow_ref == case.workflow


def test_explicit_custodian_role_and_stale_candidate(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    case.connect()
    with pytest.raises(IntakeError, match="data_custodian_required"):
        case.live.save_binding(case.owner, case.workflow, "truth")
    with case.authority.guard(case.custodian, case.workflow) as scope:
        expected = binding_candidate(case.custodian, scope, OUTCOME)
    case.body["grants"][1]["revision"] = "successor-truth"
    case.write()
    before = case.live.path.read_bytes()
    with pytest.raises(IntakeError):
        case.live.save_binding(case.custodian, case.workflow, "truth", expected_revision=expected)
    assert case.live.path.read_bytes() == before
    assert case.live.current_bindings(case.owner, case.workflow)[0][1].resource == "decisions"


def test_stale_candidate_cannot_retarget_to_new_dataset(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    case.connect()
    with case.authority.guard(case.custodian, case.workflow) as scope:
        expected = binding_candidate(case.custodian, scope, OUTCOME)
    case.body["dataset_ref"] = "new-dataset"
    case.write()
    before = case.live.path.read_bytes()
    with pytest.raises(IntakeError):
        case.live.save_binding(case.custodian, case.workflow, "truth", expected_revision=expected)
    assert case.live.path.read_bytes() == before


def test_intake_catalog_before_first_saved_binding_is_read_only_empty(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    assert case.intake().discover_sources(case.actor, limit=1, cursor=None).sources == ()
    assert not case.live.path.exists()
    Path(str(case.live.path) + "-journal").touch(mode=0o600)
    with pytest.raises(IntakeError):
        case.intake().discover_sources(case.actor, limit=1, cursor=None)
    assert not case.live.path.exists()


@pytest.mark.parametrize(
    "fault", ["foreign", "old_epoch", "expired", "paused", "unknown_actor", "bad_seal"]
)
def test_current_private_authority_denies_before_admission(tmp_path: Path, fault: str):
    case = SignedCase(tmp_path / "worker")
    reference = case.connect()
    if fault == "foreign":
        case.body["tenant"] = "org-b"
    elif fault == "old_epoch":
        case.body["engagement_epoch"] = "old-epoch"
    elif fault == "expired":
        case.body["expires_at"] = 1
    elif fault == "paused":
        case.body["state"] = "paused"
    elif fault == "unknown_actor":
        del case.body["actors"]["worker"]
    case.write()
    if fault == "bad_seal":
        _write_private(case.authority.path, {"grant": case.body, "seal_sha256": "0" * 64})
    before = (case.directory / "intake.sqlite3").read_bytes()
    with pytest.raises(IntakeError):
        case.intake().submit_intake(case.actor, request(reference))
    assert (case.directory / "intake.sqlite3").read_bytes() == before


def test_emitted_live_request_uses_real_store_and_closed_wire_shape(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    reference = case.connect()
    frames = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "generated-live", "version": "1"},
            },
        },
        {"method": "notifications/initialized"},
        {
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "submit_intake",
                "arguments": {
                    "mode": "live",
                    "feed_binding_ref": reference,
                    "source_revision": "source-v1",
                    "idempotency_key": "actual-wire",
                },
            },
        },
        {
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "submit_intake",
                "arguments": {
                    "mode": "live",
                    "feed_binding_ref": reference,
                    "source_revision": "source-v1",
                    "idempotency_key": "injected",
                    "actor": "data-custodian",
                },
            },
        },
    ]
    incoming = io.BytesIO(
        "".join(json.dumps({"jsonrpc": "2.0", **v}) + "\n" for v in frames).encode()
    )
    outgoing = io.BytesIO()
    assert serve_intake(case.actor, case.intake(), incoming, outgoing) == 0
    responses = {v["id"]: v for v in map(json.loads, outgoing.getvalue().splitlines())}
    assert responses[2]["result"]["structuredContent"]["run_ref"].startswith("run-")
    assert responses[3]["result"]["isError"] is True


async def qualified_sdk():
    """Separate actual official-SDK subprocess witness; no network/provider calls."""
    import sys
    import tempfile
    from importlib.metadata import version

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="live-sdk-") as temporary:
        case = SignedCase(Path(temporary).resolve() / "worker")
        selected = case.directory / "selected-live.json"
        _write_private(
            selected,
            {
                "schema_version": SELECTION_SCHEMA,
                "tenant": "org-a",
                "workflow_ref": case.workflow,
                "actor": "worker",
            },
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "opentour_cell.pilot.intake_local",
                str(selected),
            ],
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        admitted = None
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "discover_sources",
                "submit_intake",
                "get_run",
            }
            empty = await client.call_tool("discover_sources", {})
            assert empty.structuredContent["sources"] == []
            assert not case.live.path.exists()
            reference = case.connect()
            catalog = await client.call_tool("discover_sources", {})
            assert catalog.structuredContent["sources"][0]["feed_binding_ref"] == reference
            for key in ("first", "duplicate", "retry"):
                result = await client.call_tool(
                    "submit_intake",
                    {
                        "mode": "live",
                        "feed_binding_ref": reference,
                        "source_revision": "source-v1",
                        "idempotency_key": key,
                    },
                )
                assert result.isError is False
                if admitted is None:
                    admitted = result.structuredContent
                assert result.structuredContent == admitted
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("get_run", {"run_ref": admitted["run_ref"]})
            assert result.structuredContent["workflow_ref"] == case.workflow
            assert result.structuredContent["reason_code"] == "live_reader_unavailable"
            case.body["grants"][0]["revision"] = "successor"
            case.write()
            successor = case.connect()
            held = await client.call_tool(
                "submit_intake",
                {
                    "mode": "live",
                    "feed_binding_ref": reference,
                    "source_revision": "source-v1",
                    "idempotency_key": "first",
                },
            )
            assert held.structuredContent["reason_code"] == "binding_superseded"
            assert held.structuredContent["successor_feed_binding_ref"] == successor
            assert "run_ref" not in held.structuredContent
            case.body["state"] = "revoked"
            case.write()
            denied = await client.call_tool(
                "submit_intake",
                {
                    "mode": "live",
                    "feed_binding_ref": reference,
                    "source_revision": "source-v1",
                    "idempotency_key": "first",
                },
            )
            assert denied.isError and admitted["run_ref"] not in denied.model_dump_json()
        with case.live._db() as db:
            assert db.execute("SELECT count(*) FROM live_events").fetchone()[0] == 0
        return {
            "sdk_version": version("mcp"),
            "sessions": 2,
            "redeliveries": 3,
            "durable_receipt": True,
            "superseded_held": True,
            "revoked_invisible": True,
            "source_reads": 0,
            "events": 0,
        }


if __name__ == "__main__":
    import asyncio

    print(json.dumps(asyncio.run(qualified_sdk()), sort_keys=True))


def test_signed_engagement_gate_holds_without_ledger_and_after_withdrawal(tmp_path: Path):
    case = SignedCase(tmp_path / "worker", enroll=False)
    case.body["engagement_ledger_required"] = True
    case.write()
    epoch = case.body["engagement_epoch"]

    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        case.connect()
    assert not (case.directory / "engagement-epochs.sqlite3").exists()
    assert not case.live.path.exists()

    ledger = EngagementLedger(case.directory)
    ledger.record_engagement(
        "org-a",
        epoch,
        lease_ref=DECISION.lease_ref,
        authority_digest="a" * 64,
        resources=(DECISION.resource, OUTCOME.resource),
        lineage_ref="generated-trusted-cut",
        checkpoint=DECISION.checkpoint,
        budget_limit=100,
    )
    ledger.activate("org-a", epoch)
    assert case.connect()
    ledger.withdraw("org-a", epoch, revocation_epoch=1)

    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        case.connect()


def _ingest_authority_page(case, reference, page_ref, positions):
    from test_live_evidence import decision

    metadata = dict(
        interval_start=None,
        interval_end=None,
        coverage_through=None,
        complete=True,
        next_cursor=None,
    )
    events = tuple(decision(f"{page_ref}-{p}") for p in positions)
    return case.live.ingest_page(
        case.actor,
        reference,
        page_ref=page_ref,
        source_revision=DECISION.source_revision,
        events=events,
        source_positions=tuple(positions),
        event_epochs=tuple(case.body["engagement_epoch"] for _ in positions),
        checkpoint=max(positions),
        read_metadata=metadata,
    )


def _archived_markers(case):
    with case.live._db() as db:
        rows = db.execute("SELECT body FROM live_snapshots ORDER BY sequence").fetchall()
    return [json.loads(r[0]).get("epoch_enforcement") for r in rows]


def test_grant_without_field_is_refused_with_actionable_code_and_writes_nothing(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    del case.body["engagement_ledger_required"]
    case.write()
    before = {p.name: p.read_bytes() for p in case.directory.iterdir() if p.is_file()}
    with pytest.raises(IntakeError) as refused:
        case.connect()
    message = str(refused.value)
    assert message.startswith("engagement_ledger_required_missing")
    assert '"engagement_ledger_required": true' in message
    assert not case.live.path.exists()
    # Same refusal at guard entry (the read paths) and from _document directly.
    for operation in (
        lambda: case.authority._document(),
        lambda: case.authority.guard(case.actor, case.workflow).__enter__(),
    ):
        with pytest.raises(IntakeError, match="engagement_ledger_required_missing"):
            operation()
    # Even with an enrolled, active ledger present, absent is a refusal, not a pass.
    after = {p.name: p.read_bytes() for p in case.directory.iterdir() if p.is_file()}
    assert before == after


def test_unsealed_grant_without_field_gets_no_guidance(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    del case.body["engagement_ledger_required"]
    envelope = {"grant": case.body, "seal_sha256": "0" * 64}
    _write_private(case.directory / f"live-authority-{case.workflow}.json", envelope)
    with pytest.raises(IntakeError, match="^live_evidence_unavailable$"):
        case.authority._document()


@pytest.mark.parametrize(
    "value", [False, 0, 1, "true", None, "temporary_opt_out", "", "applied", [True]]
)
def test_false_and_unrecognised_flag_values_are_refused(tmp_path: Path, value):
    case = SignedCase(tmp_path / "worker")
    case.body["engagement_ledger_required"] = value
    case.write()
    with pytest.raises(IntakeError, match="^live_evidence_unavailable$"):
        case.authority._document()
    with pytest.raises(IntakeError):
        case.connect()
    assert not case.live.path.exists()


def test_grant_with_true_enforces_and_records_applied(tmp_path: Path):
    case = SignedCase(tmp_path / "worker")
    assert case.body["engagement_ledger_required"] is True
    epoch = case.body["engagement_epoch"]
    ledger = case.ledger
    reference = case.connect()
    _ingest_authority_page(case, reference, "page-a", [31, 5])
    reads = case.live.read_receipts(case.custodian, case.workflow)
    assert [r["epoch_enforcement"] for r in reads] == ["applied"]
    assert _archived_markers(case) == ["applied"]
    # Existing enforcement is unchanged: the at-or-before-checkpoint row was quarantined and
    # the ledger recorded only the admitted position.
    assert case.live.snapshot(case.custodian, case.workflow).quarantined == 1
    assert ledger.get("org-a", epoch).observed_high == 31
    # The snapshot path surfaces and validates the same marker as read_receipts.
    archived = case.live.snapshot(case.custodian, case.workflow, as_of_revision=1)
    assert archived.epoch_enforcement == "applied"
    assert case.live.snapshot(case.custodian, case.workflow).epoch_enforcement == "not_evaluated"

    def retag(expression):
        with case.live._db(write=True) as db:
            db.execute(f"UPDATE live_snapshots SET body={expression}")

    retag("json_set(body,'$.epoch_enforcement','bogus')")
    with pytest.raises(IntakeError):
        case.live.snapshot(case.custodian, case.workflow, as_of_revision=1)
    retag("json_remove(body,'$.epoch_enforcement')")
    legacy = case.live.snapshot(case.custodian, case.workflow, as_of_revision=1)
    assert legacy.epoch_enforcement == "not_recorded"
    retag("json_set(body,'$.epoch_enforcement','not_required_by_grant')")
    old = case.live.snapshot(case.custodian, case.workflow, as_of_revision=1)
    assert old.epoch_enforcement == "not_required_by_grant"
    retag("json_set(body,'$.epoch_enforcement','applied')")
    ledger.withdraw("org-a", epoch, revocation_epoch=1)
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        _ingest_authority_page(case, reference, "page-b", [40])


def test_every_production_live_evidence_site_takes_the_authority_default():
    """No site passes its own `engagements`, so each one inherits PrivateLiveAuthority's gate."""
    import ast

    root = Path(__file__).resolve().parents[2] / "src" / "opentour_cell"
    sites = []
    for source in root.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "LiveEvidence"
            ):
                sites.append((source.name, node))
    assert sorted(name for name, _ in sites) == [
        "live_authority.py",
        "live_outcome_history.py",
        "local_decision_context.py",
        "readiness_selection.py",
        "slack_adapter.py",
        "slack_adapter.py",
    ]
    for name, node in sites:
        assert len(node.args) == 2 and not node.keywords, name
        authority = ast.unparse(node.args[1])
        assert authority in {"authority", "self.authority"}, (name, authority)
