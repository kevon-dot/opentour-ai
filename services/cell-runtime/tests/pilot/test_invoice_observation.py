"""Independent generated producer signatures; the app receives only a public key."""

import base64
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest
from opentour_cell.pilot import repair_bundle as bundle
from test_intake_mcp import _write_private
from test_invoice_decision_emitter import EXAMPLE, attempt, emit, export_page, scope_file


@pytest.fixture(scope="module")
def producer_keys(tmp_path_factory):
    directory = tmp_path_factory.mktemp("independent-model-producer").resolve()
    directory.chmod(0o700)
    private = directory / "producer.pem"
    public = directory / "producer.public.pem"
    for args in (
        ["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)],
        ["pkey", "-in", str(private), "-pubout", "-out", str(public)],
    ):
        subprocess.run([bundle._openssl(), *args], check=True, capture_output=True)
    private.chmod(0o600)
    public.chmod(0o600)
    return private, public


@pytest.fixture
def sample(tmp_path, producer_keys):
    scope_path = scope_file(tmp_path.resolve() / "application")
    scope = json.loads(scope_path.read_text())
    private, public = producer_keys
    key = scope_path.parent / "producer.public.pem"
    key.write_bytes(public.read_bytes())
    key.chmod(0o600)
    selected = dict(
        schema_version="opentour.example.invoice-observation-selection.v1",
        grant_ref="observed-model-v1",
        **{
            k: scope[k]
            for k in ("tenant", "workflow", "engagement_epoch", "namespace", "policy_ref")
        },
        producer_ref="independent-registered-cpu",
        model_ref="invoice-model-M1",
        model_revision="sha256-" + "a" * 64,
        public_key_file=key.name,
        public_key_sha256=hashlib.sha256(key.read_bytes()).hexdigest(),
        expires_at=int(time.time()) + 600,
    )
    _write_private(scope_path.parent / "invoice-observation-selection.json", selected)
    claim = dict(
        schema_version="opentour.example.invoice-observation.v1",
        **{
            k: v
            for k, v in selected.items()
            if k not in {"schema_version", "public_key_file", "public_key_sha256", "expires_at"}
        },
        operation="operation-D1",
        attempt="attempt-2",
        decision="D1",
        case="generated-invoice-1",
        family="generated-supplier-1",
        decision_at=100,
        action="supplies",
        input_ref="observed-input-D1",
        receipt_ref="producer-receipt-D1",
    )
    sign(scope_path, claim, private)
    emit(scope_path, "initialize")
    attempt(scope_path, "operation-D1", "attempt-1")
    attempt(scope_path, "operation-D1", "attempt-2")
    spec = importlib.util.spec_from_file_location("invoice_observation_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    journal = module.InvoiceJournal(scope_path.parent, module.Scope.load(scope_path))
    return scope_path, selected, claim, private, journal, module


def sign(scope, claim, private):
    signature = bundle._crypto(
        ["dgst", "-sha256", "-sign", str(private)], bundle._bytes(claim), "fixture_sign_failed"
    )
    packet = dict(claim=claim, signature=base64.b64encode(signature).decode("ascii"))
    _write_private(scope.parent / "observation-D1.json", packet)


def commit_observed(scope, *, receipt="observation-D1.json", success=True):
    return emit(
        scope,
        "commit",
        "--operation",
        "operation-D1",
        "--attempt",
        "attempt-2",
        "--decision",
        "D1",
        "--case",
        "generated-invoice-1",
        "--family",
        "generated-supplier-1",
        "--action",
        "supplies",
        "--decision-at",
        "100",
        "--observation",
        receipt,
        success=success,
    )


def args(claim):
    return {
        k: claim[k]
        for k in ("operation", "attempt", "decision", "case", "family", "action", "decision_at")
    } | {"observation": "observation-D1.json"}


def count(scope):
    with sqlite3.connect(scope.parent / "invoice-decisions.sqlite3") as db:
        return db.execute("SELECT count(*) FROM commits").fetchone()[0]


def test_signed_receipt_normal_cli_retry_and_opaque_export(sample):
    scope, _, claim, _, _, _ = sample
    saved = commit_observed(scope)
    assert commit_observed(scope) == saved
    assert count(scope) == 1
    _, page = export_page(scope, "observed-page", 10, 11)
    [row] = page["rows"]
    event = row["event"]
    assert event["action_ref"] == "supplies"
    assert event["input_ref"] == claim["input_ref"] and event["input_fidelity"] == "observed"
    assert event["model_ref"] == claim["model_ref"] and event["actor_ref"] == claim["producer_ref"]
    assert event["source_provenance"].startswith("invoice-observed-")
    assert set(event) == {
        "kind",
        "event_id",
        "event_revision",
        "decision_id",
        "decision_revision",
        "case_id",
        "family_id",
        "decision_at",
        "action_ref",
        "policy_ref",
        "source_provenance",
        "input_ref",
        "input_fidelity",
        "actor_ref",
        "model_ref",
    }
    assert all("PRIVATE KEY" not in p.read_text() for p in scope.parent.glob("*.pem"))


def test_default_commit_never_reads_observation_or_key(sample, monkeypatch):
    scope, _, claim, _, journal, _ = sample
    monkeypatch.setattr(
        bundle, "_read", lambda *a: pytest.fail("Default commit read capture metadata")
    )
    journal.commit(**{k: v for k, v in args(claim).items() if k != "observation"})
    page = journal.export_page("default-page", 10, 11)
    [row] = json.loads((scope.parent / page["file"]).read_text())["rows"]
    assert row["event"]["input_fidelity"] == "unavailable"
    assert row["event"]["input_ref"] is row["event"]["model_ref"] is None


@pytest.mark.parametrize(
    "field",
    [
        "tenant",
        "workflow",
        "engagement_epoch",
        "namespace",
        "policy_ref",
        "grant_ref",
        "producer_ref",
        "model_ref",
        "model_revision",
        "operation",
        "attempt",
        "decision",
        "case",
        "family",
        "action",
        "decision_at",
    ],
)
def test_independently_signed_wrong_binding_cannot_commit(sample, field):
    scope, _, claim, private, _, _ = sample
    claim[field] = 101 if field == "decision_at" else "other-ref"
    sign(scope, claim, private)
    commit_observed(scope, success=False)
    assert count(scope) == 0


@pytest.mark.parametrize(
    "bad",
    [
        "missing_selection",
        "expiry",
        "withdrawal",
        "scope",
        "key_pin",
        "signature",
        "raw_fields",
        "traversal",
        "symlink",
        "permissions",
    ],
)
def test_no_opt_in_or_tampering_has_zero_committed_effect(sample, bad):
    scope, selected, claim, private, _, _ = sample
    selection_path = scope.parent / "invoice-observation-selection.json"
    receipt = "observation-D1.json"
    if bad == "missing_selection":
        selection_path.unlink()
    elif bad in {"expiry", "scope", "key_pin"}:
        field, value = {
            "expiry": ("expires_at", 1),
            "scope": ("workflow", "foreign-flow"),
            "key_pin": ("public_key_sha256", "0" * 64),
        }[bad]
        selected[field] = value
        _write_private(selection_path, selected)
    elif bad == "withdrawal":
        (scope.parent / "invoice-observation-withdrawn-observed-model-v1.json").touch(mode=0o600)
    elif bad == "signature":
        path = scope.parent / receipt
        packet = json.loads(path.read_text())
        packet["signature"] = base64.b64encode(b"generated-not-a-signature").decode()
        _write_private(path, packet)
    elif bad == "raw_fields":
        claim["inputs"] = {"text": "GENERATED_PRIVATE_INVOICE_PAYLOAD"}
        sign(scope, claim, private)
    elif bad == "traversal":
        receipt = "../observation-D1.json"
    elif bad == "symlink":
        (scope.parent / "receipt-alias.json").symlink_to(scope.parent / receipt)
        receipt = "receipt-alias.json"
    else:
        (scope.parent / receipt).chmod(0o644)
    commit_observed(scope, receipt=receipt, success=False)
    assert count(scope) == 0


@pytest.mark.parametrize("when", ["db_open", "after_insert", "after_signature"])
@pytest.mark.parametrize("change", ["withdrawal", "selection", "key", "receipt"])
def test_current_authority_and_immutable_receipt_rechecked_inside_commit(
    sample, monkeypatch, when, change
):
    scope, selected, claim, private, journal, module = sample

    def mutate():
        if change == "withdrawal":
            (scope.parent / "invoice-observation-withdrawn-observed-model-v1.json").touch(
                mode=0o600
            )
        elif change == "selection":
            _write_private(
                scope.parent / "invoice-observation-selection.json",
                selected | {"expires_at": selected["expires_at"] + 1},
            )
        elif change == "key":
            (scope.parent / selected["public_key_file"]).write_bytes(b"different-public-key")
        else:
            path = scope.parent / "observation-D1.json"
            path.write_bytes(path.read_bytes() + b"\n")

    original = journal._connection
    if when in {"db_open", "after_insert"}:

        class Connection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, parameters=()):
                result = self.db.execute(sql, parameters)
                if sql.startswith("INSERT INTO commits"):
                    mutate()
                return result

        @contextmanager
        def changed(*, write):
            with original(write=write) as db:
                if when == "db_open":
                    mutate()
                yield Connection(db) if when == "after_insert" else db

        monkeypatch.setattr(journal, "_connection", changed)
    else:
        crypto = bundle._crypto

        def changed_crypto(*args):
            result = crypto(*args)
            mutate()
            return result

        monkeypatch.setattr(bundle, "_crypto", changed_crypto)
    with pytest.raises((ValueError, OSError)):
        journal.commit(**args(claim))
    assert count(scope) == 0


def test_changed_independent_receipt_cannot_rewrite_committed_provenance(sample):
    scope, selected, claim, private, _, _ = sample
    commit_observed(scope)
    claim["receipt_ref"] = "different-producer-receipt"
    sign(scope, claim, private)
    commit_observed(scope, success=False)
    assert count(scope) == 1
    claim["model_revision"] = "sha256-" + "b" * 64
    selected["model_revision"] = claim["model_revision"]
    _write_private(scope.parent / "invoice-observation-selection.json", selected)
    sign(scope, claim, private)
    commit_observed(scope, success=False)
    assert count(scope) == 1


@pytest.mark.parametrize("permission", ["withdrawn", "expired", "foreign"])
def test_current_opt_in_precedes_observation_body_read(sample, monkeypatch, permission):
    scope, selected, claim, _, journal, _ = sample
    if permission == "withdrawn":
        (scope.parent / "invoice-observation-withdrawn-observed-model-v1.json").touch(mode=0o600)
    else:
        selected["expires_at" if permission == "expired" else "workflow"] = (
            1 if permission == "expired" else "another-workflow"
        )
        _write_private(scope.parent / "invoice-observation-selection.json", selected)
    original = bundle._read
    reads = []

    def read(path, *a):
        if path.name == "observation-D1.json":
            reads.append(path)
        return original(path, *a)

    monkeypatch.setattr(bundle, "_read", read)
    with pytest.raises(ValueError):
        journal.commit(**args(claim))
    assert reads == [] and count(scope) == 0


def test_crypto_failure_emits_no_raw_diagnostic(sample, monkeypatch, capsys):
    scope, _, _, _, _, module = sample

    def timed_out(*a):
        raise subprocess.TimeoutExpired("GENERATED_PRIVATE_INVOICE_PAYLOAD", 30)

    monkeypatch.setattr(bundle, "_crypto", timed_out)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(EXAMPLE),
            "--scope",
            str(scope),
            "commit",
            "--operation",
            "operation-D1",
            "--attempt",
            "attempt-2",
            "--decision",
            "D1",
            "--case",
            "generated-invoice-1",
            "--family",
            "generated-supplier-1",
            "--action",
            "supplies",
            "--decision-at",
            "100",
            "--observation",
            "observation-D1.json",
        ],
    )
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err == "invoice_decision_emitter_unavailable\n"
    assert count(scope) == 0
