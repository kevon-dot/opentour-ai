"""Actual signed authority and durable resolver through the scoped intake transport."""

import hmac
import io
import json
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, parse_intake, serve_intake
from opentour_cell.pilot.live_authority import SCHEMA, LocalLiveIntake, PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence, _canonical, binding_candidate
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from test_intake_mcp import STARTER, _starter_only, _write_private
from test_live_evidence import DECISION, OUTCOME

KEY = bytes.fromhex("6a" * 32)


class SignedCase:
    def __init__(self, path: Path):
        selection_path, selection = _starter_only(path)
        self.actor, admission = open_intake(selection_path)
        admitted = admission.submit_intake(self.actor, parse_intake(STARTER))
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
        }
        _write_private(path / "live-authority-key.json", {"key": KEY.hex()})
        self.write()
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
