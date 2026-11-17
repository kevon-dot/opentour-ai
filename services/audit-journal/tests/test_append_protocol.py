"""Generated crypto and simulated AWS responses; these do not qualify real AWS/IAM."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import shutil
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import httpx2 as httpx
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.stub import Stubber
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentour_audit.journal import (
    ApprovedRegistration,
    AuthorizedScope,
    AWSJournalStore,
    JournalConfiguration,
    JournalService,
    ReconciledTransaction,
    ResolvedIntent,
    ServicePrincipal,
    create_router,
)
from opentour_control.authority.journal_client import (
    HTTPSJournalTransport,
    JournalClient,
    RetryClaim,
    TransportResponse,
)
from opentour_journal.crypto import (
    P256_ORDER,
    JournalVerifier,
    TrustedKey,
    TrustPolicy,
    canonical,
    create_signature,
    der_to_raw,
    model_digest,
    raw_to_der,
    sha256,
    transcript,
)
from opentour_journal.models import (
    APPEND_PURPOSE,
    HEAD_PURPOSE,
    MAX_COUNTER,
    MAX_MANIFEST_BYTES,
    MAX_REVISION,
    AppendIntent,
    AppendOutcome,
    AppendReceiptEnvelope,
    ImmutableMetadataRef,
    JournalError,
    JournalIdentity,
    JournalMetric,
    MetadataRef,
    ReplayCheckpoint,
    ReplayManifest,
    emit_metric,
    strict_json,
)
from opentour_journal.replay import FrontierVerifier, VerifiedAuthorityFrontier

NOW = datetime(2026, 11, 16, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def test_authority_ports_are_reexported_without_duplicate_types() -> None:
    from opentour_audit import journal
    from opentour_journal import ports

    for name in (
        "ServicePrincipal",
        "AuthorizedScope",
        "ResolvedIntent",
        "ReconciledTransaction",
        "ApprovedRegistration",
        "AuthorityResolver",
    ):
        assert getattr(journal, name) is getattr(ports, name)


def test_authority_ports_import_without_service_or_provider_modules() -> None:
    probe = """
import importlib.abc
import sys

class RefuseServiceImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {
            'opentour_audit', 'opentour_control', 'boto3', 'botocore', 'fastapi',
        }:
            raise ImportError('service import forbidden in pure authority ports')

sys.meta_path.insert(0, RefuseServiceImports())
from opentour_journal.ports import AuthorityResolver, ServicePrincipal
assert AuthorityResolver.__module__ == 'opentour_journal.ports'
assert ServicePrincipal.__module__ == 'opentour_journal.ports'
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr


class ConditionalFailure(ClientError):
    pass


class TransactionFailure(ClientError):
    pass


def fail(exception):
    return exception({"Error": {"Code": exception.__name__, "Message": "generated fault"}}, "test")


class SimulatedDynamoDB:
    exceptions = SimpleNamespace(
        ConditionalCheckFailedException=ConditionalFailure,
        TransactionCanceledException=TransactionFailure,
    )

    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.calls = []
        self.crash_before = self.lose_ack = False

    def get_item(self, **request):
        assert request["ConsistentRead"] is True
        with self.lock:
            self.calls.append(("get_item", request))
            key = request["Key"]
            row = self.rows.get((key["PK"], key["SK"]))
            return {"Item": copy.deepcopy(row)} if row else {}

    def put_item(self, **request):
        with self.lock:
            self.calls.append(("put_item", request))
            item = request["Item"]
            key = item["PK"], item["SK"]
            if key in self.rows:
                raise fail(ConditionalFailure)
            self.rows[key] = copy.deepcopy(item)

    def transact_write_items(self, **request):
        with self.lock:
            self.calls.append(("transact_write_items", request))
            if self.crash_before:
                raise ReadTimeoutError(endpoint_url="https://generated.invalid")
            actions = request["TransactItems"]
            assert len(actions) == 3
            update = actions[0]["Update"]
            key = update["Key"]["PK"], update["Key"]["SK"]
            current = self.rows[key]["head"]
            values = update["ExpressionAttributeValues"]
            expected = [
                current[name]
                for name in (
                    "sequence",
                    "chain_sha256",
                    "revision",
                    "unresolved_operation_id",
                )
            ]
            if [values[f":v{i}"] for i in range(4)] != expected:
                raise fail(TransactionFailure)
            new_rows = [action["Put"]["Item"] for action in actions[1:]]
            if any((r["PK"], r["SK"]) in self.rows for r in new_rows):
                raise fail(TransactionFailure)
            self.rows[key]["head"] = copy.deepcopy(values[":new_head"])
            for row in new_rows:
                self.rows[row["PK"], row["SK"]] = copy.deepcopy(row)
            if self.lose_ack:
                raise ReadTimeoutError(endpoint_url="https://generated.invalid")
            return {}


class SimulatedS3:
    def __init__(self, encryption_key):
        self.objects = {}
        self.lock = threading.Lock()
        self.encryption_key = encryption_key
        self.calls = []
        self.last_body = None

    def put_object(self, **request):
        with self.lock:
            version = f"version-{len(self.objects) + 1}"
            self.calls.append(("put_object", request))
            self.objects[request["Key"], version] = request["Body"]
            assert (
                request["ChecksumSHA256"]
                == base64.b64encode(bytes.fromhex(sha256(request["Body"]))).decode()
            )
            return {"VersionId": version}

    def get_object(self, **request):
        self.calls.append(("get_object", request))
        self.last_body = io.BytesIO(self.objects[request["Key"], request["VersionId"]])
        return {
            "Body": self.last_body,
            "VersionId": request["VersionId"],
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": self.encryption_key,
        }


class SimulatedKMS:
    def __init__(self, key):
        self.private_key = key
        self.calls = []
        self.failure = False

    def sign(self, **request):
        self.calls.append(request)
        assert request["MessageType"] == "DIGEST"
        assert request["SigningAlgorithm"] == "ECDSA_SHA_256"
        assert len(request["Message"]) == 32
        if self.failure:
            raise ReadTimeoutError(endpoint_url="https://generated.invalid")
        return {
            "KeyId": request["KeyId"],
            "SigningAlgorithm": "ECDSA_SHA_256",
            "Signature": self.private_key.sign(
                request["Message"], ec.ECDSA(utils.Prehashed(hashes.SHA256()))
            ),
        }


def metadata(name):
    return ImmutableMetadataRef(
        record_id=name, revision="version-one", sha256=sha256(name.encode())
    )


def artifact(kind, data):
    return MetadataRef(
        artifact_id="generated-artifact",
        kind=kind,
        revision="version-one",
        sha256=sha256(data),
        manifest_id="generated-manifest",
        environment_id="generated-audit",
        classification="control_metadata",
        purpose_grant_id="generated-evidence-purpose",
    )


class SimulatedAuthority:
    def authorize(self, principal, org, capability):
        if principal.service_id != "generated-control":
            raise JournalError("JOURNAL_FORBIDDEN", 403)
        if org not in {"org-a", "org-b"}:
            raise JournalError("JOURNAL_NOT_FOUND", 404)
        return AuthorizedScope(principal.service_id, org, capability)

    def resolve_intent(self, principal, body):
        return ResolvedIntent(
            **{name: getattr(body, name) for name in ResolvedIntent.__dataclass_fields__}
        )

    def reconcile_transaction(self, principal, body, intent):
        return ReconciledTransaction(
            body.organization_id,
            body.operation_id,
            model_digest(intent),
            body.control_transaction_ref,
            body.result,
            body.resulting_epoch,
            body.resulting_authority_revision,
        )

    def registration(self, principal, org):
        return ApprovedRegistration(org, 6, "authority-six", metadata("enrollment"))


class GeneratedCheckpoints:
    def __init__(self, journal, heads):
        self.checkpoints = {
            h.organization_id: ReplayCheckpoint(
                journal=journal,
                organization_id=h.organization_id,
                sequence=h.sequence,
                chain_sha256=h.chain_sha256,
                latest_epoch=h.latest_epoch,
                latest_authority_revision=h.latest_authority_revision,
                evidence_ref=artifact("journal_checkpoint", canonical(h.wire())),
            )
            for h in heads
        }

    def load(self, org):
        return self.checkpoints[org]


class GeneratedEvidence:
    def __init__(self):
        self.bytes = []

    def write(self, org, kind, data):
        self.bytes.append((org, kind, data))
        return artifact(kind, data)


class GeneratedTelemetry:
    def __init__(self):
        self.events = []
        self.failure = False

    def record(self, metric):
        if self.failure:
            raise OSError("generated sink diagnostic")
        self.events.append(metric)


class GeneratedTransport:
    def __init__(self, service, principal):
        self.service, self.principal = service, principal

    def post(self, path, body):
        value = strict_json(body)
        match path.rsplit("/", 2)[-2:]:
            case ["authority-journal", "intents"]:
                status, response = self.service.append(self.principal, AppendIntent.parse(body))
                return TransportResponse(status, canonical(response.wire()))
            case ["authority-journal", "outcomes"]:
                status, response = self.service.append(self.principal, AppendOutcome.parse(body))
                return TransportResponse(status, canonical(response.wire()))
            case ["heads", "read"]:
                response = self.service.read_live_head(
                    self.principal, value["organization_id"], value["request_nonce"]
                )
            case ["entries", "read"]:
                response = self.service.read_entry(
                    self.principal, value["organization_id"], value["sequence"]
                )
            case ["operations", "read"]:
                response = self.service.read_operation(
                    self.principal, value["organization_id"], value["operation_id"], value["phase"]
                )
            case _:
                raise AssertionError("unregistered generated path")
        return TransportResponse(200, canonical(response.wire()))


def build_system():
    private = ec.generate_private_key(ec.SECP256R1())
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key = TrustedKey(
        "arn:aws:kms:us-east-1:111111111111:key/00000000-0000-0000-0000-000000000001",
        "registry-one",
        der,
        sha256(der),
        frozenset({APPEND_PURPOSE, HEAD_PURPOSE}),
        True,
    )
    journal = JournalIdentity(
        audit_account_id="111111111111",
        region="us-east-1",
        journal_instance_id="generated-journal",
        configuration_digest="a" * 64,
    )
    trust = TrustPolicy(journal, "generated-journal-content", (key,))
    config = JournalConfiguration(
        trust,
        "generated-journal-table",
        "arn:aws:kms:us-east-1:111111111111:key/00000000-0000-0000-0000-000000000002",
        "arn:aws:iam::111111111111:role/generated-journal-append",
        key,
        "COMPLIANCE",
        30,
        7,
        7,
        30,
    )
    ddb, s3, kms = (
        SimulatedDynamoDB(),
        SimulatedS3(config.encryption_key_arn),
        SimulatedKMS(private),
    )
    store = AWSJournalStore(config, ddb, s3, kms)
    authority = SimulatedAuthority()
    telemetry = GeneratedTelemetry()
    service = JournalService(store, authority, telemetry, lambda: NOW)
    principal = ServicePrincipal("generated-control", metadata("authentication"))
    heads = [service.register(principal, org) for org in ["org-a", "org-b"]]
    checkpoints = GeneratedCheckpoints(journal, heads)
    client = JournalClient(
        GeneratedTransport(service, principal), JournalVerifier(trust), checkpoints, telemetry
    )
    return SimpleNamespace(
        store=store,
        service=service,
        principal=principal,
        client=client,
        ddb=ddb,
        s3=s3,
        kms=kms,
        authority=authority,
        evidence=GeneratedEvidence(),
        checkpoints=checkpoints,
        telemetry=telemetry,
    )


@pytest.fixture
def system():
    return build_system()


def intent(system, operation="operation-a", org="org-a", kind="restrict", epoch=None):
    head = system.store.head(org)
    return AppendIntent(
        organization_id=org,
        operation_id=operation,
        expected_head_sequence=head.sequence,
        expected_head_digest=head.chain_sha256,
        mutation_kind=kind,
        proposed_epoch=epoch if epoch is not None else head.latest_epoch + 1,
        proposed_authority_revision=f"authority-{operation}",
        canonical_intent_digest="c" * 64,
        scoped_intent_ref=metadata("intent"),
        actor_ref=metadata("actor"),
        created_at="2026-11-16T00:00:00.000Z",
    )


def outcome(system, body, result="committed"):
    head = system.store.head(body.organization_id)
    receipt = system.store.operation(body.organization_id, body.operation_id, "intent")
    return AppendOutcome(
        organization_id=body.organization_id,
        operation_id=body.operation_id,
        expected_head_sequence=head.sequence,
        expected_head_digest=head.chain_sha256,
        intent_sequence=receipt.sequence,
        result=result,
        control_transaction_ref=metadata("sql-receipt"),
        resulting_epoch=body.proposed_epoch if result == "committed" else head.latest_epoch,
        resulting_authority_revision=body.proposed_authority_revision
        if result == "committed"
        else head.latest_authority_revision,
    )


def test_twenty_concurrent_intents_one_unresolved_and_other_org_independent(system):
    bodies = [intent(system, f"operation-{i}") for i in range(20)]

    def append(body):
        try:
            return system.service.append(system.principal, body)[0]
        except JournalError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(append, bodies))
    assert results.count(201) == 1
    assert set(results) <= {201, "AUTHORITY_OPERATION_PENDING"}
    assert system.store.head("org-a").sequence == 1
    assert system.client.append_intent(intent(system, org="org-b")).sequence == 1


def test_original_signed_receipt_replays_without_clock_or_signature_dependency(system):
    body = intent(system)
    original = system.client.append_intent(body)
    system.kms.failure = True
    assert canonical(system.client.append_intent(body).wire()) == canonical(original.wire())
    assert len(system.kms.calls) == 1
    assert system.store.head("org-a").sequence == 1


def test_changed_body_same_operation_conflicts(system):
    body = intent(system)
    system.client.append_intent(body)
    with pytest.raises(JournalError, match="JOURNAL_OPERATION_CONFLICT"):
        system.client.append_intent(body.model_copy(update={"canonical_intent_digest": "d" * 64}))


@pytest.mark.parametrize("fault", ["before_transaction", "signing", "lost_ack"])
def test_s3_and_transaction_crash_boundaries(system, fault):
    body = intent(system)
    system.ddb.crash_before = fault == "before_transaction"
    system.ddb.lose_ack = fault == "lost_ack"
    system.kms.failure = fault == "signing"
    if fault == "lost_ack":
        response = system.client.append_intent(body)
        assert response.sequence == 1
        assert system.client.append_intent(body) == response
    else:
        with pytest.raises(JournalError, match="JOURNAL_UNAVAILABLE"):
            system.service.execute(lambda: system.service.append(system.principal, body))
        assert system.store.head("org-a").sequence == 0
    assert len(system.s3.objects) == 1


@pytest.mark.parametrize("kind", ["grant", "restrict"])
def test_unresolved_never_authorizes_and_proven_abort_releases_only_prior_state(system, kind):
    body = intent(system, kind=kind)
    system.client.append_intent(body)
    frontier = system.client.verify_frontier("org-a", system.evidence)
    assert frontier.unresolved_kind == kind
    assert frontier.head.latest_epoch == 6
    with pytest.raises(JournalError, match="AUTHORITY_OPERATION_PENDING"):
        frontier.require_resolved()
    system.client.append_outcome(outcome(system, body, "aborted"))
    restored = system.client.verify_frontier("org-a", system.evidence)
    restored.require_resolved()
    assert restored.head.latest_epoch == 6


def test_unproven_abort_cannot_clear_head(system):
    body = intent(system)
    system.client.append_intent(body)
    system.authority.reconcile_transaction = lambda *args: None
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        system.client.append_outcome(outcome(system, body, "aborted"))
    assert system.store.head("org-a").unresolved_operation_id == body.operation_id


def test_restore_six_replays_seven_eight_and_exact_restrictive_inclusion(system):
    first = intent(system)
    receipt = system.client.append_intent(first)
    system.client.append_outcome(outcome(system, first))
    second = intent(system, "operation-b")
    system.client.append_intent(second)
    system.client.append_outcome(outcome(system, second))
    frontier = system.client.verify_frontier("org-a", system.evidence)
    assert frontier.head.latest_epoch == 8 and frontier.head.sequence == 4
    inclusion = frontier.restrictive_inclusion(
        first.operation_id, receipt.sequence, receipt.chain_sha256
    )
    assert inclusion.outcome_sequence == 2
    with pytest.raises(JournalError, match="JOURNAL_INCLUSION_UNPROVEN"):
        frontier.restrictive_inclusion("other", receipt.sequence, receipt.chain_sha256)
    assert system.s3.last_body.closed


@pytest.mark.parametrize(
    "fault",
    ["missing_sequence", "swapped_version", "altered_content", "checkpoint", "head_unavailable"],
)
def test_recovery_adverse_states_hold(system, fault):
    body = intent(system)
    system.client.append_intent(body)
    system.client.append_outcome(outcome(system, body))
    key = ("ORG#org-a", "SEQ#00000000000000000001")
    if fault == "missing_sequence":
        del system.ddb.rows[key]
    elif fault == "swapped_version":
        value = json.loads(system.ddb.rows[key]["entry"])
        value["content_version_id"] = "version-other"
        system.ddb.rows[key]["entry"] = json.dumps(value)
    elif fault == "altered_content":
        first = next(iter(system.s3.objects))
        system.s3.objects[first] = b"{}"
    elif fault == "checkpoint":
        old = system.checkpoints.checkpoints["org-a"]
        system.checkpoints.checkpoints["org-a"] = old.model_copy(update={"chain_sha256": "f" * 64})
    else:
        system.service.read_live_head = lambda *args: (_ for _ in ()).throw(
            JournalError("JOURNAL_UNAVAILABLE", 503)
        )
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        system.client.verify_frontier("org-a", system.evidence)


@pytest.mark.parametrize(
    "wire",
    [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1.0}', b'{"a":1e0}', b'{"a":"\\ud800"}', b"\xff"],
)
def test_strict_wire_rejects_ambiguous_bytes(wire):
    with pytest.raises(JournalError, match="SCHEMA_INVALID"):
        strict_json(wire)


@pytest.mark.parametrize(
    "field,value",
    [
        ("proposed_epoch", True),
        ("proposed_epoch", MAX_COUNTER + 1),
        ("proposed_epoch", -1),
        ("workflow_payload", "generated-private-sentinel"),
        ("created_at", "2026-11-16T00:00:00Z"),
    ],
)
def test_closed_schema_rejects_raw_payload_and_coercions(system, field, value):
    data = intent(system).wire()
    data[field] = value
    with pytest.raises(JournalError, match="SCHEMA_INVALID"):
        AppendIntent.parse(json.dumps(data).encode())


def test_jcs_utf16_order_and_known_escaping():
    # RFC8785 sorts property names by UTF-16, not Python Unicode code points.
    assert (
        canonical({"\ue000": "x", "\U0001f600": "y", "a": "\n"})
        == '{"a":"\\n","😀":"y","\ue000":"x"}'.encode()
    )


@pytest.mark.parametrize("r,s", [(0, 1), (1, 0), (P256_ORDER, 1), (1, P256_ORDER)])
def test_es256_scalar_range(r, s):
    with pytest.raises(JournalError, match="JOURNAL_SIGNATURE_INVALID"):
        der_to_raw(utils.encode_dss_signature(r, s))


def test_es256_canonical_padding_and_trailing_bits(system):
    receipt = system.client.append_intent(intent(system))
    encoded = receipt.signed_receipt.signature.signature
    assert der_to_raw(raw_to_der(encoded)) == encoded
    with pytest.raises(JournalError):
        raw_to_der(encoded + "=")


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", "org-b"),
        ("operation_id", "foreign"),
        ("content_version_id", "swapped"),
        ("content_bucket_ref", "foreign-bucket"),
        ("chain_sha256", "e" * 64),
    ],
)
def test_receipt_binding_or_signature_tamper_refused(system, field, value):
    body = intent(system)
    receipt = system.client.append_intent(body)
    changed = receipt.signed_receipt.model_copy(
        update={"payload": receipt.signed_receipt.payload.model_copy(update={field: value})}
    )
    with pytest.raises(JournalError):
        system.client.verifier.receipt(changed, "org-a", body.operation_id, "intent")


def test_nonce_bound_head_cannot_be_replayed(system):
    head = system.client.read_live_head("org-a", "a" * 64)
    with pytest.raises(JournalError, match="JOURNAL_BINDING_INVALID"):
        system.client.verifier.live_head(head, "org-a", "b" * 64)


def test_foreign_service_scope_and_registration_refuse(system):
    foreign = ServicePrincipal("foreign", metadata("foreign"))
    with pytest.raises(JournalError, match="JOURNAL_FORBIDDEN"):
        system.service.append(foreign, intent(system))
    with pytest.raises(JournalError, match="JOURNAL_NOT_FOUND"):
        system.service.read_live_head(system.principal, "foreign-org", "a" * 64)
    with pytest.raises(JournalError, match="JOURNAL_REGISTRATION_CONFLICT"):
        system.service.register(system.principal, "org-a")


@pytest.mark.parametrize(
    "field,value", [("sequence", MAX_COUNTER), ("revision", str(MAX_REVISION))]
)
def test_counter_exhaustion_before_s3(system, field, value):
    system.ddb.rows["ORG#org-a", "HEAD"]["head"][field] = value
    with pytest.raises(JournalError, match="REVISION_EXHAUSTED"):
        system.client.append_intent(intent(system))
    assert not system.s3.objects


def test_runtime_verified_frontier_cannot_be_constructed_from_untrusted_boolean(system):
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        VerifiedAuthorityFrontier(
            True, system.store.head("org-a"), artifact("proof", b"x"), None, ()
        )


def test_actual_fastapi_factory_admits_configured_identity_and_sanitizes_fault(system):
    async def authenticated(request):
        return system.principal

    app = FastAPI()
    app.include_router(create_router(system.service, authenticated))
    with TestClient(app) as client:
        response = client.post("/v1/authority-journal/intents", json=intent(system).wire())
        assert response.status_code == 201
        assert (
            client.post(
                "/v1/authority-journal/intents", json={"secret": "generated-private"}
            ).status_code
            == 422
        )
        system.store.head = lambda *args: (_ for _ in ()).throw(RuntimeError("generated-private"))
        failed = client.post(
            "/v1/authority-journal/heads/read",
            json={"organization_id": "org-a", "request_nonce": "a" * 64},
        )
        assert failed.status_code == 503 and "generated-private" not in failed.text


def test_actual_sdk_resource_transaction_request_has_three_distinct_items(system):
    # Stubber exercises maintained SDK serialization/model validation without a cloud call.
    ddb = boto3.resource(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id="generated",
        aws_secret_access_key="generated",
    ).meta.client
    body = intent(system)
    before = system.store.head("org-a")
    response = system.client.append_intent(body)
    after = system.store.head("org-a")
    entry = system.store.entry("org-a", 1)
    store = AWSJournalStore(system.store.config, ddb, system.s3, system.kms)
    with Stubber(ddb) as stub:
        stub.add_response("transact_write_items", {})
        store.commit(before, after, entry, response)
        stub.assert_no_pending_responses()
    transaction = next(
        request for method, request in system.ddb.calls if method == "transact_write_items"
    )
    assert [list(action) for action in transaction["TransactItems"]] == [
        ["Update"],
        ["Put"],
        ["Put"],
    ]
    ddb.close()


def test_approved_retention_is_required_not_defaulted(system):
    with pytest.raises(JournalError, match="JOURNAL_CONFIGURATION_INVALID"):
        replace(system.store.config, retention_days=13).validate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("key_id", "unapproved"),
        ("key_registry_revision", "unapproved"),
        ("payload_sha256", "f" * 64),
        ("transcript_sha256", "f" * 64),
    ],
)
def test_signer_selectors_and_hashes_are_not_response_authority(system, field, value):
    body = intent(system)
    response = system.client.append_intent(body)
    signature = response.signed_receipt.signature.model_copy(update={field: value})
    with pytest.raises(JournalError):
        system.client.verifier.receipt(
            response.signed_receipt.model_copy(update={"signature": signature}),
            "org-a",
            body.operation_id,
            "intent",
        )


def test_cross_purpose_signature_is_structurally_refused(system):
    body = intent(system)
    response = system.client.append_intent(body).signed_receipt.wire()
    response["signature"]["purpose"] = HEAD_PURPOSE
    with pytest.raises(JournalError, match="SCHEMA_INVALID"):
        AppendReceiptEnvelope.parse(json.dumps(response).encode())


def test_independent_account_or_instance_cannot_be_replaced(system):
    trust = system.store.config.trust
    for field, value in [("audit_account_id", "222222222222"), ("journal_instance_id", "restored")]:
        different = replace(trust, journal=trust.journal.model_copy(update={field: value}))
        with pytest.raises(JournalError):
            verifier = JournalVerifier(different)
            head = system.client.read_live_head("org-a", "a" * 64)
            verifier.live_head(head, "org-a", "a" * 64)


def test_head_advancement_during_replay_restarts_then_has_finite_limit(system):
    original = system.service.read_live_head
    calls = 0

    def advancing(*args):
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            body = intent(system, f"advance-{calls}")
            system.client.append_intent(body)
            system.client.append_outcome(outcome(system, body))
        return original(*args)

    system.service.read_live_head = advancing
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        system.client.verify_frontier("org-a", system.evidence)
    assert calls == 4 and not system.evidence.bytes


def test_head_advances_once_and_replay_includes_new_restrictive_pair(system):
    original = system.service.read_live_head
    calls = 0

    def advancing_once(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            body = intent(system)
            system.client.append_intent(body)
            system.client.append_outcome(outcome(system, body))
        return original(*args)

    system.service.read_live_head = advancing_once
    verified = system.client.verify_frontier("org-a", system.evidence)
    assert calls == 4 and verified.head.sequence == 2


@pytest.mark.parametrize(
    "fault", ["short_retention", "versioning", "encryption", "pitr", "ttl", "signer"]
)
def test_actual_activation_checks_required_storage_settings(system, fault):
    config = system.store.config
    lock = {
        "ObjectLockEnabled": "Enabled",
        "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 30}},
    }
    versioning = {"Status": "Enabled"}
    encryption = {
        "Rules": [
            {
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "aws:kms",
                    "KMSMasterKeyID": config.encryption_key_arn,
                }
            }
        ]
    }
    pitr = {"PointInTimeRecoveryStatus": "ENABLED"}
    ttl = {"TimeToLiveStatus": "DISABLED"}
    public = {
        "KeyId": config.signing_key.key_id,
        "KeySpec": "ECC_NIST_P256",
        "KeyUsage": "SIGN_VERIFY",
        "SigningAlgorithms": ["ECDSA_SHA_256"],
        "PublicKey": config.signing_key.public_key_der,
    }
    table = {
        "TableArn": "arn:aws:dynamodb:us-east-1:111111111111:table/generated-journal-table",
        "TableStatus": "ACTIVE",
        "SSEDescription": {"KMSMasterKeyArn": config.encryption_key_arn, "Status": "ENABLED"},
    }
    system.s3.get_object_lock_configuration = lambda **kw: {"ObjectLockConfiguration": lock}
    system.s3.get_bucket_versioning = lambda **kw: versioning
    system.s3.get_bucket_encryption = lambda **kw: {"ServerSideEncryptionConfiguration": encryption}
    system.ddb.describe_table = lambda **kw: {"Table": table}
    system.ddb.describe_continuous_backups = lambda **kw: {
        "ContinuousBackupsDescription": {"PointInTimeRecoveryDescription": pitr}
    }
    system.ddb.describe_time_to_live = lambda **kw: {"TimeToLiveDescription": ttl}
    system.kms.get_public_key = lambda **kw: public
    system.store.verify_storage_configuration()
    if fault == "short_retention":
        lock["Rule"]["DefaultRetention"]["Days"] = 1
    elif fault == "versioning":
        versioning["Status"] = "Suspended"
    elif fault == "encryption":
        encryption["Rules"][0]["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"] = "foreign"
    elif fault == "pitr":
        pitr["PointInTimeRecoveryStatus"] = "DISABLED"
    elif fault == "ttl":
        ttl["TimeToLiveStatus"] = "ENABLED"
    else:
        public["PublicKey"] = b"foreign"
    with pytest.raises(JournalError, match="JOURNAL_CONFIGURATION_INVALID"):
        system.store.verify_storage_configuration()


def test_object_write_uses_verified_bucket_default_without_retention_mutation_permission(system):
    system.client.append_intent(intent(system))
    request = system.s3.calls[0][1]
    assert (
        not {"ObjectLockMode", "ObjectLockRetainUntilDate", "BypassGovernanceRetention"}
        & request.keys()
    )
    assert request["ExpectedBucketOwner"] == "111111111111"


def test_future_or_backward_replay_clock_holds(system):
    times = iter([10.0, 9.0])
    system.client.frontier_verifier.monotonic = lambda: next(times)
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        system.client.verify_frontier("org-a", system.evidence)


def test_missing_operation_is_strong_observation_not_an_aborted_result(system):
    observed = system.client.read_exact_operation("org-a", "missing", "intent")
    assert observed.state == "absent_after_strong_read" and observed.response is None
    assert system.ddb.calls[-1][1]["ConsistentRead"] is True


def test_same_operation_outcome_cannot_target_another_intent(system):
    body = intent(system)
    system.client.append_intent(body)
    wrong = outcome(system, body).model_copy(update={"intent_sequence": 2})
    with pytest.raises(JournalError):
        system.client.append_outcome(wrong)
    assert system.store.head("org-a").unresolved_operation_id == body.operation_id


def test_schema_examples_resolve_only_registered_common_primitives(system):
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource

    schema = json.loads((ROOT / "packages/contracts/authority-journal.schema.json").read_text())
    common = json.loads(
        (ROOT / "packages/contracts/common/common-contract.schema.json").read_text()
    )
    registry = Registry().with_resources(
        [(s["$id"], Resource.from_contents(s)) for s in [schema, common]]
    )
    body = intent(system)
    response = system.client.append_intent(body)
    head = system.client.read_live_head("org-a")
    system.client.verify_frontier("org-a", system.evidence)
    examples = [
        ("AppendIntent", body.wire()),
        ("AppendResponse", response.wire()),
        ("LiveHeadEnvelope", head.wire()),
    ]
    for _, kind, data in system.evidence.bytes:
        value = strict_json(data)
        examples.append(
            (
                "ReplayManifest" if kind == "journal_replay_manifest" else "VerifiedFrontierProof",
                value if kind == "journal_replay_manifest" else value["payload"],
            )
        )
    for name, value in examples:
        Draft202012Validator(
            {"$ref": schema["$id"] + "#/$defs/" + name},
            registry=registry,
            format_checker=FormatChecker(),
        ).validate(value)


def test_twenty_two_entry_manifest_roundtrip_and_exact_evidence_budget(system):
    for number in range(11):
        body = intent(system, f"operation-{number}")
        system.client.append_intent(body)
        system.client.append_outcome(outcome(system, body))
    frontier = system.client.verify_frontier("org-a", system.evidence)
    assert frontier.head.sequence == 22
    data = next(
        data for _, kind, data in system.evidence.bytes if kind == "journal_replay_manifest"
    )
    assert len(data) > 65536
    parsed = ReplayManifest.parse(data)
    assert len(parsed.observations) == 22 and canonical(parsed.wire()) == data
    verifier = FrontierVerifier(
        system.client.verifier, system.checkpoints, system.telemetry, max_manifest_bytes=len(data)
    )
    assert verifier.verify("org-a", system.client, GeneratedEvidence()).head == frontier.head
    writer = GeneratedEvidence()
    verifier = FrontierVerifier(
        system.client.verifier,
        system.checkpoints,
        system.telemetry,
        max_manifest_bytes=len(data) - 1,
    )
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        verifier.verify("org-a", system.client, writer)
    assert writer.bytes == []


def test_manifest_parse_has_separate_finite_boundary(system):
    system.client.verify_frontier("org-a", system.evidence)
    data = system.evidence.bytes[0][2]
    padded = data + b" " * (MAX_MANIFEST_BYTES - len(data))
    assert ReplayManifest.parse(padded) == ReplayManifest.parse(data)
    with pytest.raises(JournalError, match="SCHEMA_INVALID"):
        ReplayManifest.parse(padded + b" ")
    # The larger evidence budget never applies to the service request parser.
    with pytest.raises(JournalError, match="SCHEMA_INVALID"):
        strict_json(data + b" " * 65536)


def test_manifest_budget_stops_entry_accumulation_before_evidence(system):
    body = intent(system)
    system.client.append_intent(body)
    system.client.append_outcome(outcome(system, body))
    reads = []
    original = system.client.read_entry

    def read(org, sequence):
        reads.append(sequence)
        return original(org, sequence)

    system.client.read_entry = read
    verifier = FrontierVerifier(
        system.client.verifier, system.checkpoints, system.telemetry, max_manifest_bytes=100
    )
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        verifier.verify("org-a", system.client, system.evidence)
    assert reads == [1] and system.evidence.bytes == []


@pytest.mark.parametrize("status,code", [(422, "SCHEMA_INVALID"), (409, "JOURNAL_HEAD_CONFLICT")])
def test_client_preserves_canonical_error_status(system, status, code):
    system.client.transport.post = lambda *args: TransportResponse(
        status, canonical({"code": code, "message": "generated diagnostic"})
    )
    with pytest.raises(JournalError) as caught:
        system.client.append_intent(intent(system))
    assert caught.value.code == code and caught.value.status == status
    assert "generated diagnostic" not in str(caught.value)


@pytest.mark.parametrize("data", [b"[", b'{"code":null}', b'{"code":"not-approved"}'])
def test_client_malformed_error_remains_unavailable(system, data):
    system.client.transport.post = lambda *args: TransportResponse(422, data)
    with pytest.raises(JournalError) as caught:
        system.client.append_intent(intent(system))
    assert (caught.value.code, caught.value.status) == ("JOURNAL_UNAVAILABLE", 503)


@pytest.mark.parametrize("checkpoint_sequence", [0, 2])
def test_corrupt_head_revision_cannot_verify_empty_replay(system, checkpoint_sequence):
    if checkpoint_sequence:
        body = intent(system)
        system.client.append_intent(body)
        system.client.append_outcome(outcome(system, body))
        system.client.frontier_verifier.checkpoints = GeneratedCheckpoints(
            system.store.config.trust.journal, [system.store.head("org-a")]
        )
    system.ddb.rows["ORG#org-a", "HEAD"]["head"]["revision"] = "99"
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        system.client.verify_frontier("org-a", system.evidence)
    assert system.evidence.bytes == []
    with pytest.raises(JournalError, match="JOURNAL_STATE_INVALID"):
        system.client.append_intent(intent(system, "new-operation"))


def test_first_signed_entry_revision_must_follow_genesis(system):
    body = intent(system)
    response = system.client.append_intent(body)
    payload = response.signed_receipt.payload.model_copy(update={"head_revision": "99"})
    signed = AppendReceiptEnvelope(
        payload=payload,
        signature=create_signature(
            payload, APPEND_PURPOSE, system.store.config.signing_key, system.store.sign
        ),
    )
    # The trusted signer really signed the wrong revision; signature validity cannot fix it.
    with pytest.raises(JournalError, match="JOURNAL_BINDING_INVALID"):
        system.client.verifier.receipt(signed, "org-a", body.operation_id, "intent")


def test_signed_genesis_head_revision_must_be_one(system):
    from opentour_journal.models import LiveHeadEnvelope

    nonce = "a" * 64
    head = system.client.read_live_head("org-a", nonce)
    payload = head.payload.model_copy(
        update={"head": head.payload.head.model_copy(update={"revision": "99"})}
    )
    envelope = LiveHeadEnvelope(
        payload=payload,
        signature=create_signature(
            payload, HEAD_PURPOSE, system.store.config.signing_key, system.store.sign
        ),
    )
    with pytest.raises(JournalError, match="JOURNAL_BINDING_INVALID"):
        system.client.verifier.live_head(envelope, "org-a", nonce)


class SimulatedDurableRetry:
    """Generated port double; actual143 SQL claim/CAS remains a separate required gate."""

    def __init__(self):
        self.row = None
        self.events = []
        self.lose_complete_ack = False

    def claim(self, binding, body, now):
        previous = self.row
        if previous:
            if previous.binding != binding or previous.original_body != body:
                raise JournalError("JOURNAL_OPERATION_CONFLICT")
            if self.state == "claimed":
                raise JournalError("AUTHORITY_OPERATION_PENDING")
            if now < self.due:
                raise JournalError("JOURNAL_RETRY_NOT_DUE")
        claim = RetryClaim(
            binding, body, "generated-claim", str(previous.attempt + 1 if previous else 1),
            previous.attempt + 1 if previous else 1,
            previous.first_attempt_at if previous else now,
            self.due if previous else now,
        )
        self.row, self.state = claim, "claimed"
        self.events.append(("claim", claim))
        return claim

    def schedule(self, claim, not_before, code):
        assert claim is self.row and self.state == "claimed"
        self.state, self.due = "scheduled", not_before
        self.events.append(("schedule", not_before, code))

    def complete(self, claim, response):
        assert claim is self.row and self.state == "claimed"
        self.state, self.due = "acknowledged", claim.not_before
        self.events.append(("complete", response))
        if self.lose_complete_ack:
            raise OSError("generated commit acknowledgement loss")

    def hold(self, claim, code):
        assert claim is self.row and self.state == "claimed"
        self.state, self.due = "held", claim.not_before
        self.events.append(("hold", code))


@pytest.mark.parametrize("lost_after_commit", [False, True])
def test_durable_retry_reconciles_exact_operation_before_one_stable_attempt(
    system, lost_after_commit
):
    durable = SimulatedDurableRetry()
    body = intent(system)
    original = system.client.transport.post
    writes = []

    def fault(path, data):
        if path.endswith("/intents"):
            writes.append(data)
            if lost_after_commit:
                original(path, data)
            raise OSError("generated transport loss")
        return original(path, data)

    system.client.transport.post = fault
    first = system.client.retry_append(body, durable, clock=lambda: NOW)
    assert first.state == "retry_scheduled" and first.response is None
    assert first.not_before == NOW + timedelta(seconds=1)
    assert durable.row.original_body == canonical(body.wire())
    system.client.transport.post = original
    second = system.client.retry_append(body, durable, clock=lambda: first.not_before)
    assert second.state == "acknowledged" and second.response.sequence == 1
    assert len(system.s3.objects) == 1 and system.store.head("org-a").sequence == 1
    assert writes == [canonical(body.wire())]
    assert [e[0] for e in durable.events] == ["claim", "schedule", "claim", "complete"]


@pytest.mark.parametrize("attempt,sample,delay", [(1, 1, 1), (2, 1, 2), (6, 1, 30), (90, 0, 1)])
def test_retry_jitter_is_durable_one_to_thirty_seconds(system, attempt, sample, delay):
    durable = SimulatedDurableRetry()
    original_claim = durable.claim

    def claim(*args):
        durable.row = replace(original_claim(*args), attempt=attempt)
        return durable.row

    durable.claim = claim
    system.client.transport.post = lambda *args: (_ for _ in ()).throw(OSError("generated loss"))
    result = system.client.retry_append(
        intent(system), durable, clock=lambda: NOW, jitter=lambda: sample
    )
    assert result.not_before == NOW + timedelta(seconds=delay)
    assert durable.events[-1][0] == "schedule"
    assert not system.s3.objects


def test_retry_never_changes_body_or_rewrites_stale_head(system):
    durable = SimulatedDurableRetry()
    body = intent(system)
    original = system.client.transport.post
    system.client.transport.post = lambda *args: (_ for _ in ()).throw(OSError("generated loss"))
    result = system.client.retry_append(body, durable, clock=lambda: NOW)
    changed = body.model_copy(update={"canonical_intent_digest": "d" * 64})
    with pytest.raises(JournalError, match="JOURNAL_OPERATION_CONFLICT"):
        system.client.retry_append(changed, durable, clock=lambda: result.not_before)
    with pytest.raises(JournalError, match="JOURNAL_RETRY_NOT_DUE"):
        system.client.retry_append(body, durable, clock=lambda: NOW)
    system.client.transport.post = original
    system.client.append_intent(intent(system, "different-operation"))
    with pytest.raises(JournalError, match="AUTHORITY_OPERATION_PENDING"):
        system.client.retry_append(body, durable, clock=lambda: result.not_before)
    assert durable.state == "held" and durable.row.original_body == canonical(body.wire())
    assert system.store.head("org-a").sequence == 1


def test_retry_complete_ack_loss_cannot_return_success_or_repeat_append(system):
    durable = SimulatedDurableRetry()
    durable.lose_complete_ack = True
    body = intent(system)
    with pytest.raises(JournalError, match="JOURNAL_RETRY_STATE_UNAVAILABLE"):
        system.client.retry_append(body, durable, clock=lambda: NOW)
    assert system.store.head("org-a").sequence == 1
    durable.lose_complete_ack = False
    recovered = system.client.retry_append(body, durable, clock=lambda: NOW)
    assert recovered.state == "acknowledged" and len(system.s3.objects) == 1


@pytest.mark.parametrize("fault", ["backwards", "jitter_nan", "schedule_ack_loss"])
def test_retry_unknown_clock_or_persistence_never_claims_scheduled_success(system, fault):
    durable = SimulatedDurableRetry()
    system.client.transport.post = lambda *args: (_ for _ in ()).throw(OSError("generated loss"))
    times = iter([NOW, NOW - timedelta(seconds=1) if fault == "backwards" else NOW])
    if fault == "schedule_ack_loss":
        durable.schedule = lambda *args: (_ for _ in ()).throw(OSError("generated sink body"))
    with pytest.raises(JournalError):
        system.client.retry_append(
            intent(system), durable, clock=lambda: next(times),
            jitter=lambda: float("nan") if fault == "jitter_nan" else 0,
        )
    assert durable.state == "claimed" and not system.s3.objects


def test_retry_durable_binding_fence_precedes_all_network(system):
    durable = SimulatedDurableRetry()
    original = durable.claim

    def wrong_claim(*args):
        return replace(original(*args), original_body=b"{}")

    durable.claim = wrong_claim
    system.client.transport.post = lambda *args: pytest.fail("invalid claim reached network")
    with pytest.raises(JournalError, match="JOURNAL_RETRY_STATE_INVALID"):
        system.client.retry_append(intent(system), durable, clock=lambda: NOW)


def test_unresolved_age_uses_service_time_and_alert_never_resolves(system):
    body = intent(system).model_copy(update={"created_at": "2000-01-01T00:00:00.000Z"})
    system.client.append_intent(body)
    for seconds in [599, 600, 3600]:
        system.service.clock = lambda seconds=seconds: NOW + timedelta(seconds=seconds)
        system.service.observe_unresolved(system.principal, ("org-a", "org-b"))
    alerts = [
        e.value for e in system.telemetry.events if e.name == "unresolved_intent_over_10_minutes"
    ]
    assert alerts == [600, 3600]
    assert system.store.head("org-a").unresolved_operation_id == body.operation_id
    assert system.store.head("org-a").latest_epoch == 6
    assert all(set(vars(event)) == {"name", "value"} for event in system.telemetry.events)


def test_monitor_clock_regression_is_not_negative_age_or_resolution(system):
    body = intent(system)
    system.client.append_intent(body)
    system.service.clock = lambda: NOW - timedelta(seconds=1)
    with pytest.raises(JournalError, match="JOURNAL_TIME_UNAVAILABLE"):
        system.service.observe_unresolved(system.principal, ("org-a",))
    assert system.store.head("org-a").unresolved_operation_id == body.operation_id


def test_required_telemetry_failure_after_append_is_unknown_until_exact_reconciliation(system):
    body = intent(system)
    system.telemetry.failure = True
    with pytest.raises(JournalError, match="JOURNAL_OBSERVABILITY_UNAVAILABLE") as caught:
        system.client.append_intent(body)
    assert caught.value.__suppress_context__
    assert "generated sink diagnostic" not in str(caught.value)
    assert system.store.head("org-a").sequence == 1
    system.telemetry.failure = False
    replay = system.client.append_intent(body)
    assert replay.sequence == 1 and len(system.s3.objects) == 1


def test_known_orphan_bytes_and_unknown_transaction_are_not_conflated(system):
    system.kms.failure = True
    with pytest.raises(JournalError):
        system.client.append_intent(intent(system))
    orphan = [e.value for e in system.telemetry.events if e.name == "orphan_bytes"]
    assert orphan == [len(canonical(intent(system).wire()))]
    system.kms.failure = False
    system.ddb.crash_before = True
    with pytest.raises(JournalError):
        system.client.append_intent(intent(system))
    # A transport timeout alone does not prove the second candidate is an orphan.
    assert [e.value for e in system.telemetry.events if e.name == "orphan_bytes"] == orphan


def test_missing_sequence_and_explicit_issuance_denial_are_counted(system):
    body = intent(system)
    system.client.append_intent(body)
    with pytest.raises(JournalError, match="AUTHORITY_OPERATION_PENDING"):
        system.client.require_issuance_frontier("org-a", system.evidence)
    del system.ddb.rows["ORG#org-a", "SEQ#00000000000000000001"]
    with pytest.raises(JournalError, match="RECOVERY_HOLD"):
        system.client.require_issuance_frontier("org-a", system.evidence)
    assert sum(e.value for e in system.telemetry.events if e.name == "denied_issuance_count") == 2
    assert JournalMetric("missing_sequence_count", 1) in system.telemetry.events


@pytest.mark.parametrize(
    "name,value", [("private-label", 1), ("orphan_bytes", -1), ("orphan_bytes", float("nan"))]
)
def test_telemetry_rejects_unknown_names_and_invalid_values_before_sink(system, name, value):
    with pytest.raises(JournalError, match="JOURNAL_OBSERVABILITY_UNAVAILABLE"):
        emit_metric(system.telemetry, name, value)
    assert system.telemetry.events == []


def test_exact_append_transcript_cross_language_jcs_and_es256(system):
    node = shutil.which("node")
    assert node, "The pinned Node runtime is required for the cross-language fixture"
    receipt = system.client.append_intent(intent(system)).signed_receipt
    key = system.store.config.signing_key
    payload = {
        "public_der": base64.b64encode(key.public_key_der).decode(),
        "transcript": transcript(APPEND_PURPOSE, receipt.payload.wire()).decode(),
        "signature": receipt.signature.signature,
        "vector": {"\ue000": "x", "\U0001f600": "y", "a": "\n", "n": MAX_COUNTER},
        "vector_canonical": canonical(
            {"\ue000": "x", "\U0001f600": "y", "a": "\n", "n": MAX_COUNTER}
        ).decode(),
    }
    script = r'''
const fs = require('node:fs');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const x = JSON.parse(fs.readFileSync(0, 'utf8'));
// Restricted integer/object/string test vector; this is not a production canonicalizer.
const ordered = Object.fromEntries(Object.keys(x.vector).sort().map(k => [k, x.vector[k]]));
assert.equal(JSON.stringify(ordered), x.vector_canonical);
const key = crypto.createPublicKey({
  key: Buffer.from(x.public_der, 'base64'), type:'spki',format:'der'});
const options = {key, dsaEncoding:'ieee-p1363'};
const sig = Buffer.from(x.signature, 'base64url');
assert.equal(sig.length, 64);
assert.equal(crypto.verify('sha256', Buffer.from(x.transcript), options, sig), true);
const wrongPurpose = Buffer.from(x.transcript.replace('append-receipt', 'live-head'));
assert.equal(crypto.verify('sha256', wrongPurpose, options, sig), false);
sig[0] ^= 1;
assert.equal(crypto.verify('sha256', Buffer.from(x.transcript), options, sig), false);
process.stdout.write('cross-language-profile-verified');
'''
    result = subprocess.run(
        [node, "-e", script], input=json.dumps(payload), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
    )
    assert result.stdout == "cross-language-profile-verified"


def test_original_receipt_survives_signer_rotation_and_ten_minute_window(system):
    body = intent(system)
    original = system.client.append_intent(body)
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    previous = replace(system.store.config.signing_key, live_head_eligible=False)
    current = replace(
        previous, key_id=previous.key_id[:-1] + "3", registry_revision="registry-two",
        public_key_der=public, public_key_sha256=sha256(public), live_head_eligible=True,
    )
    trust = replace(system.store.config.trust, keys=(previous, current))
    system.store.config = replace(system.store.config, trust=trust, signing_key=current)
    system.kms.private_key = private
    system.service.verifier = system.client.verifier = JournalVerifier(trust)
    system.service.clock = lambda: NOW + timedelta(seconds=601)
    assert canonical(system.client.append_intent(body).wire()) == canonical(original.wire())
    head = system.client.read_live_head("org-a")
    assert head.signature.key_id == current.key_id
    assert original.signed_receipt.signature.key_id == previous.key_id
    assert system.store.head("org-a").sequence == 1 and len(system.s3.objects) == 1


def test_new_valid_signature_does_not_create_an_append(system):
    body = intent(system)
    original = system.client.append_intent(body)
    replacement = AppendReceiptEnvelope(
        payload=original.signed_receipt.payload,
        signature=create_signature(
            original.signed_receipt.payload, APPEND_PURPOSE,
            system.store.config.signing_key, system.store.sign,
        ),
    )
    assert system.client.verifier.receipt(replacement, "org-a", body.operation_id, "intent") == (
        original.signed_receipt.payload
    )
    assert system.client.append_intent(body) == original
    assert system.store.head("org-a").sequence == 1


def test_conflicting_outcome_cannot_rewrite_proven_abort(system):
    body = intent(system)
    system.client.append_intent(body)
    aborted = outcome(system, body, "aborted")
    receipt = system.client.append_outcome(aborted)
    committed = aborted.model_copy(update={
        "result": "committed", "resulting_epoch": body.proposed_epoch,
        "resulting_authority_revision": body.proposed_authority_revision,
    })
    with pytest.raises(JournalError, match="JOURNAL_OPERATION_CONFLICT"):
        system.client.append_outcome(committed)
    assert system.client.append_outcome(aborted) == receipt
    assert system.store.head("org-a").latest_epoch == 6


def https_transport(**changes):
    values = {
        "origin": "https://journal.example.invalid",
        "approved_origin": "https://journal.example.invalid",
        "configuration_digest": "a" * 64,
        "approved_configuration_digest": "a" * 64,
        "authorizer": SimpleNamespace(headers=lambda *args: {"Authorization": "generated"}),
        "tls": ssl.create_default_context(),
    }
    values.update(changes)
    return HTTPSJournalTransport(**values)


@pytest.mark.parametrize("changes", [
    {"origin": "https://foreign.example.invalid"},
    {"origin": "http://journal.example.invalid", "approved_origin": "http://journal.example.invalid"},
    {"origin": "https://user@journal.example.invalid", "approved_origin": "https://user@journal.example.invalid"},
    {"configuration_digest": "b" * 64}, {"timeout": 0}, {"timeout": 31},
])
def test_https_transport_cannot_override_approved_configuration(changes):
    with pytest.raises(JournalError, match="JOURNAL_CONFIGURATION_INVALID"):
        https_transport(**changes)


class GeneratedAsyncBody(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay = chunks, delay
        self.closed, self.seen = False, 0

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.seen += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("fault", ["oversized", "encoding", "late", "redirect"])
def test_https_transport_bounds_response_closes_and_refuses_redirect(fault, monkeypatch):
    times = iter([0.0, 11.0 if fault == "late" else 1.0])
    transport = https_transport(monotonic=lambda: next(times))
    stream = GeneratedAsyncBody([b"x" * 65537 if fault == "oversized" else b"{}"])
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            302 if fault == "redirect" else 200, stream=stream,
            headers={"Content-Encoding": "gzip" if fault == "encoding" else "identity",
                     "Location": "https://foreign.invalid"},
        )

    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        assert kwargs["verify"].check_hostname and kwargs["verify"].verify_mode == ssl.CERT_REQUIRED
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    try:
        if fault == "redirect":
            assert transport.post("/v1/authority-journal/heads/read", b"{}").status == 302
        else:
            with pytest.raises(JournalError, match="JOURNAL_UNAVAILABLE"):
                transport.post("/v1/authority-journal/heads/read", b"{}")
    finally:
        assert transport.close()
    assert stream.closed
    assert calls == ["https://journal.example.invalid/v1/authority-journal/heads/read"]


@pytest.mark.parametrize("stall", ["headers", "body"])
def test_total_deadline_cancels_continuing_drip_and_header_wait(stall, monkeypatch):
    stream = GeneratedAsyncBody([b"x"] * 200, delay=0.02)
    cancelled = threading.Event()

    async def handler(request):
        if stall == "headers":
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return httpx.Response(200, stream=stream)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        **kw, transport=httpx.MockTransport(handler)
    ))
    transport = https_transport(timeout=0.05)
    began = time.monotonic()
    try:
        with pytest.raises(JournalError, match="JOURNAL_UNAVAILABLE"):
            transport.post("/v1/authority-journal/heads/read", b"{}")
        assert time.monotonic() - began < 0.4
    finally:
        assert transport.close()
    if stall == "body":
        assert stream.closed and stream.seen < 10
    else:
        assert cancelled.is_set()


def test_timeout_retains_bounded_capacity_until_uncancelable_work_exits(monkeypatch):
    released = threading.Event()
    calls = []
    lock = threading.Lock()

    def headers(*args):
        with lock:
            calls.append(args)
        assert released.wait(5), "Test did not release blocked authorizer"
        return {"Authorization": "generated"}

    # Models native blocking work. Every expired task must recheck BEFORE starting HTTP.
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("late HTTP dispatch"))
    transport = https_transport(timeout=0.03, authorizer=SimpleNamespace(headers=headers))
    try:
        for _ in range(4):
            with pytest.raises(JournalError, match="JOURNAL_UNAVAILABLE"):
                transport.post("/v1/authority-journal/intents", b"{}")
        assert len(calls) == 4
        started = time.monotonic()
        with pytest.raises(JournalError, match="JOURNAL_UNAVAILABLE"):
            transport.post("/v1/authority-journal/intents", b"{}")
        assert time.monotonic() - started < 0.1 and len(calls) == 4
        assert transport.close(timeout=0.01) is False
    finally:
        released.set()
        assert transport.close(timeout=1) is True
    with pytest.raises(JournalError, match="JOURNAL_UNAVAILABLE"):
        transport.post("/v1/authority-journal/intents", b"{}")
