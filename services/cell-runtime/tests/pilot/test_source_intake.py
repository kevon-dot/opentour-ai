"""Exercise the owner-local trace/correction contract with generated files and real custody."""

import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from opentour_cell.pilot import source_intake as intake
from opentour_cell.pilot.intake_local import SELECTION_VERSION
from opentour_cell.pilot.intake_mcp import DISCOVER, GET_RUN, SUBMIT
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
RUNTIME = Path(__file__).resolve().parents[2]
EXAMPLES = RUNTIME / "examples"


def write(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")
    path.chmod(0o600)


def export(path: Path, rows: list[Json]) -> str:
    if path.suffix == ".csv":
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {k: canonical(v) if isinstance(v, dict | list) else v for k, v in row.items()}
                )
        path.chmod(0o600)
    else:
        write(path, "".join(canonical(row) + "\n" for row in rows))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Sample:
    def __init__(self, path: Path, *, form: str = "jsonl", support: bool = False):
        path.mkdir(mode=0o700, exist_ok=True)
        self.store = LocalTrainingStore(path / "training.sqlite3", "generated-co")
        self.grant, self.output = path / "grant.json", path / "intake.json"
        self.traces, self.outcomes = path / ("traces." + form), path / ("outcomes." + form)
        self.g = json.loads((EXAMPLES / "pilot-source-grant.template.json").read_bytes())
        self.g["engagement"] = self.store.engagement_id
        self.t, self.o = [
            json.loads((EXAMPLES / f"pilot-source-{name}.json").read_bytes())
            for name in ("trace", "correction")
        ]
        if support:
            self.g.update(
                workflow="reward-claim", field="disposition", dataset_id="generated-support"
            )
            for r in self.t + self.o:
                r.update(workflow="reward-claim", field="disposition")
            for r in self.t:
                r["features"] = {"description": f"reward {r['group_id']} verified"}
                r["incumbent"]["label"] = {"purchase": "close", "loan": "wait"}[
                    r["incumbent"]["label"]
                ]
            for r in self.o:
                r["reference"] = {"purchase": "close", "loan": "wait"}[r["reference"]]
        self.save()

    def save(self) -> None:
        self.g["trace_sha256"] = export(self.traces, self.t)
        self.g["correction_sha256"] = export(self.outcomes, self.o)
        write(self.grant, canonical(self.g))

    def discover(self) -> Json:
        return intake.discover_intake(self.store, self.grant, self.traces, self.outcomes)

    def confirmation(self) -> Json:
        result = self.discover()
        return {
            "owner_id": self.g["owner_id"],
            "attestation": "att-3333333333333333",
            "class_id": result["suspected_error_classes"][0]["class_id"],
            "discovery_revision": result["discovery_revision"],
        }

    def freeze(self, confirmation: Json | None = None) -> Json:
        return intake.freeze_intake(
            self.store,
            self.grant,
            self.traces,
            self.outcomes,
            self.output,
            confirmation=confirmation,
        )


@pytest.mark.parametrize(("form", "support"), [("jsonl", False), ("csv", True)])
def test_freeze_recurring_class_and_enroll_protected_evaluation(
    tmp_path: Path, form: str, support: bool
) -> None:
    sample = Sample(tmp_path, form=form, support=support)
    out = sample.discover()
    assert out["development_count"] == out["protected_count"] == 3
    classes = out["suspected_error_classes"]
    assert len(classes) == 1 and classes[0]["count"] == classes[0]["groups"] == 2
    assert "invoice-4" not in canonical(out)
    confirmation = sample.confirmation()
    frozen = sample.freeze(confirmation)
    again = sample.freeze(confirmation)
    marker = intake.inspect_intake(sample.store, frozen["intake_revision"])
    assert marker["confirmed_error_class_id"] == confirmation["class_id"]
    assert frozen == again == intake.load_intake(sample.store, sample.output)
    assert frozen["intake_revision"] == digest(
        {k: v for k, v in frozen.items() if k != "intake_revision"}
    )
    assert frozen["protected_membership_digest"] == digest(["invoice-4", "invoice-5", "invoice-6"])
    assert not frozen["training_case_ids"]
    assert frozen["development"][0]["outcome_at"] > frozen["development"][0]["decision_at"]
    prediction = {
        frozen["protected"][0]["case_id"]: {
            "status": "automatic",
            "label": frozen["protected"][0]["reference"],
        }
    }
    enrolled = intake.supplied_manifest(
        sample.store, sample.output, prediction, error_ceiling_bps=100
    )
    summary = sample.store.register(enrolled)
    assert not summary["can_train"] and summary["mode"] == "evaluation_only"
    readout = sample.store.frozen_evaluation(frozen["dataset_id"])["readout"]
    assert readout["cohort_size"] == 3 and readout["repair"]["missing"] == 2
    with pytest.raises(ValueError, match="outside_protected"):
        intake.supplied_manifest(
            sample.store, sample.output, {"invoice-1": {"status": "missing"}}, error_ceiling_bps=100
        )
    assert sample.output.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing", "missing_outcome"),
        ("unknown", "outcome_unavailable"),
        ("contradiction", "contradictory_outcomes"),
        ("ambiguous", "ambiguous_outcomes"),
        ("many", "reused_outcome"),
        ("trace", "ambiguous_trace"),
        ("late", "verification_after_cutoff"),
        ("future", "future_decision_features"),
        ("retroactive", "facts_unavailable_at_decision"),
        ("policy", "changed_policy_or_schema"),
        ("schema", "changed_policy_or_schema"),
        ("adjudicator", "unapproved_adjudicator"),
        ("sequence", "inconsistent_outcome_time"),
        ("field", "fields_outside_grant"),
        ("late_dev", "development_label_after_split"),
    ],
)
def test_quarantine_real_reason_with_original_denominator(
    tmp_path: Path, case: str, reason: str
) -> None:
    h = Sample(tmp_path)
    t, o = h.t[0], h.o[0]
    if case == "missing":
        h.o.pop(0)
    elif case == "unknown":
        o["reference"] = None
    elif case in {"contradiction", "ambiguous"}:
        h.o.append(
            {
                **o,
                "outcome_id": "other",
                "reference": "other" if case == "contradiction" else o["reference"],
            }
        )
    elif case == "many":
        h.o[1]["outcome_id"] = o["outcome_id"]
    elif case == "trace":
        h.t.append({**t, "decision_at": 102})
    elif case == "late":
        o["verified_at"] = h.g["as_of"] + 1
    elif case == "future":
        t["features_available_at"] = t["decision_at"] + 1
    elif case == "retroactive":
        o["facts_as_of"] = t["decision_at"] + 1
    elif case == "policy":
        o["policy_version"] = "new-policy"
    elif case == "schema":
        t["source_schema_version"] = "unknown-schema"
    elif case == "adjudicator":
        o["adjudicator_id"] = "missing-authority"
    elif case == "sequence":
        o["verified_at"] = 1
    elif case == "field":
        t["features"]["extra_field"] = "outside scope"
    elif case == "late_dev":
        o["verified_at"] = h.g["split"]["cutoff"]
    h.save()
    data = h.freeze()
    assert data["excluded"]["invoice-1"] == reason
    assert data["excluded_counts"][reason] == (2 if case == "many" else 1)
    assert len(data["development"]) + len(data["protected"]) + len(data["excluded"]) == 6
    assert "outside scope" not in canonical(data)


def test_shuffled_equivalent_records_and_exact_retries(tmp_path: Path) -> None:
    h = Sample(tmp_path)
    original = h.freeze()
    h.t.reverse()
    h.o.reverse()
    h.t.append(deepcopy(h.t[-1]))
    h.o.append(deepcopy(h.o[-1]))
    h.o.append({**h.o[0], "event_id": "orphan", "outcome_id": "unmatched-ledger"})
    h.output = tmp_path / "second.json"
    h.save()
    new = h.freeze()
    assert (
        new["development"] == original["development"] and new["protected"] == original["protected"]
    )
    assert (
        new["sources"]["trace"]["records_digest"] == original["sources"]["trace"]["records_digest"]
    )
    assert new["sources"]["trace"]["sha256"] != original["sources"]["trace"]["sha256"]
    assert (
        new["sources"]["trace"]["duplicate_rows"]
        == new["sources"]["correction"]["duplicate_rows"]
        == 1
    )
    assert new["unmatched_outcome_ids"] == ["unmatched-ledger"]
    assert new["protected_membership_digest"] == original["protected_membership_digest"]


@pytest.mark.parametrize("mode", ["chronological", "heldout_customer"])
def test_transitive_groups_and_normalized_near_duplicates_do_not_cross_sides(
    tmp_path: Path, mode: str
) -> None:
    h = Sample(tmp_path)
    if mode == "heldout_customer":
        h.g["split"] = {"mode": "heldout_customer", "customers": ["client-b"]}
    h.t[3]["features"] = {"description": "LEASE equipment RENTAL"}
    h.t[1]["group_id"] = h.t[0]["group_id"]
    h.save()
    data = h.freeze()
    assert set(data["excluded"]) == {"invoice-1", "invoice-2", "invoice-4"}
    assert data["excluded_counts"] == {"group_crosses_split": 3}
    assert {x["group_id"] for x in data["development"]}.isdisjoint(
        x["group_id"] for x in data["protected"]
    )
    assert data["suspected_error_classes"] == []


def test_strict_export_scope_and_zero_eligible(tmp_path: Path) -> None:
    h = Sample(tmp_path)
    h.t[0]["organization"] = "not-this-org"
    h.save()
    with pytest.raises(ValueError, match="source_export_scope_mismatch"):
        h.freeze()
    h.t[0]["organization"] = h.g["organization"]
    h.o.clear()
    h.save()
    result = h.freeze()
    assert result["development"] == result["protected"] == result["suspected_error_classes"] == []
    assert result["excluded_counts"] == {"missing_outcome": 6}
    assert not result["error_class"] and not result["training_case_ids"]


def test_limited_rights_owner_confirmation_expiry_and_withdrawal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Sample(tmp_path)
    h.g["training_attestation"] = "att-2222222222222222"
    h.g["training_expires_at"] = int(intake.time.time()) + 300
    h.save()
    confirmation = h.confirmation()
    wrong = {**confirmation, "discovery_revision": "0" * 64}
    with pytest.raises(ValueError, match="unconfirmed_error_class"):
        h.freeze(wrong)
    wrong = {**confirmation, "class_id": "class-does-not-recur"}
    with pytest.raises(ValueError, match="unverified_recurring_error"):
        h.freeze(wrong)
    unconfirmed = h.freeze()
    with pytest.raises(ValueError, match="source_training_not_authorized"):
        intake.development_rows(h.store, unconfirmed["intake_revision"], for_training=True)
    h.output = tmp_path / "confirmed.json"
    result = h.freeze(confirmation)
    rev = result["intake_revision"]
    assert result["training_case_ids"] == ["invoice-1", "invoice-2", "invoice-3"]
    assert intake.inspect_intake(h.store, rev)["training_authorized"]
    assert intake.development_rows(h.store, rev, for_training=True) == result["development"]
    assert set(intake.protected_inputs(h.store, rev)[0]) == {"case_id", "features"}
    assert set(intake.protected_inputs(h.store, rev, include_groups=True)[0]) == {
        "case_id",
        "features",
        "group_id",
    }
    original = h.output.read_bytes()
    h.output.unlink()
    assert intake.inspect_intake(h.store, rev)["protected_count"] == 3
    assert (
        len(intake.development_rows(h.store, rev, for_training=True))
        == len(intake.protected_inputs(h.store, rev))
        == 3
    )
    write(h.output, original.decode())
    monkeypatch.setattr(intake.time, "time", lambda: h.g["training_expires_at"] + 1)
    assert not intake.inspect_intake(h.store, rev)["training_authorized"]
    with pytest.raises(ValueError, match="source_training_not_authorized"):
        intake.development_rows(h.store, rev, for_training=True)
    assert intake.load_intake(h.store, h.output)["intake_revision"] == rev
    intake.withdraw_intake(h.store, h.g["owner_attestation"])
    intake.withdraw_intake(h.store, h.g["owner_attestation"])
    h.save()
    restored = LocalTrainingStore(h.store.path, h.g["organization"])
    for call in (
        lambda: intake.load_intake(restored, h.output),
        lambda: intake.inspect_intake(restored, rev),
        lambda: intake.protected_inputs(restored, rev),
        h.discover,
    ):
        with pytest.raises(ValueError, match="source_grant_withdrawn"):
            call()


def test_scope_digest_integrity_private_access_and_whole_engagement_withdrawal(
    tmp_path: Path,
) -> None:
    h = Sample(tmp_path)
    h.traces.chmod(0o644)
    with pytest.raises(ValueError, match="private_file_required"):
        h.freeze()
    h.traces.chmod(0o600)
    h.g["trace_sha256"] = "a" * 64
    write(h.grant, canonical(h.g))
    with pytest.raises(ValueError, match="digest_mismatch"):
        h.freeze()
    h.save()
    data = h.freeze()
    rev = data["intake_revision"]
    path = tmp_path / ("source-development-" + rev + ".json")
    write(path, '{"rows":[]}')
    with pytest.raises(ValueError, match="revision_mismatch"):
        intake.development_rows(h.store, rev)
    write(h.output, canonical({**data, "protected": []}))
    with pytest.raises(ValueError, match="revision_mismatch"):
        intake.load_intake(h.store, h.output)
    marker = tmp_path / ("source-intake-" + rev + ".json")
    original = marker.read_text()
    write(marker, canonical({**json.loads(original), "confirmed_recurring_error": True}))
    with pytest.raises(ValueError, match="unverified_source_authority"):
        intake.inspect_intake(h.store, rev)
    write(marker, original)
    h.store.withdraw()
    with pytest.raises(ValueError, match="withdrawn"):
        intake.inspect_intake(h.store, rev)


def test_cli_replay_in_independent_processes(tmp_path: Path) -> None:
    h = Sample(tmp_path)
    base = [
        sys.executable,
        "-m",
        "opentour_cell.pilot.source_intake",
        "--store",
        str(h.store.path),
        "--organization",
        h.store.organization,
    ]
    files = ["--grant", str(h.grant), "--traces", str(h.traces), "--corrections", str(h.outcomes)]
    env = {**os.environ, "PYTHONPATH": str(RUNTIME / "src")}

    def run(args: list[str]) -> Json:
        result = subprocess.run(base + args, env=env, capture_output=True, text=True, check=True)
        assert not result.stderr
        return json.loads(result.stdout)

    discovery = run(["discover", *files])
    confirmation = tmp_path / "confirmation.json"
    write(
        confirmation,
        canonical({**h.confirmation(), "discovery_revision": discovery["discovery_revision"]}),
    )
    args = ["freeze", *files, "--output", str(h.output), "--confirmation", str(confirmation)]
    one = run(args)
    two = run(args)
    assert one == two == run(["inspect", "--intake", str(h.output)])
    assert (
        one["development_count"] == one["protected_count"] == 3 and one["confirmed_recurring_error"]
    )
    assert (
        run(["withdraw", "--grant-attestation", h.g["owner_attestation"]])["status"] == "withdrawn"
    )
    denied = subprocess.run(
        base + ["inspect", "--intake", str(h.output)], env=env, capture_output=True, text=True
    )
    assert denied.returncode == 1 and denied.stderr.strip() == "source_grant_withdrawn"


def test_pure_authority_check_works_under_already_held_training_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Sample(tmp_path)
    frozen = h.freeze()
    engagement = h.store.engagement_id
    with h.store.engagement_guard(engagement_id=engagement):

        def no_database(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("authorization metadata must not open SQLite")

        monkeypatch.setattr(intake.sqlite3, "connect", no_database)
        meta = intake.check_current_intake(
            tmp_path, h.store.organization, engagement, frozen["intake_revision"]
        )
        assert meta["allowed_features"] == ["description"] and not meta["training_authorized"]
        with pytest.raises(ValueError, match="training_not_authorized"):
            intake.check_current_intake(
                tmp_path,
                h.store.organization,
                engagement,
                frozen["intake_revision"],
                require_training=True,
            )


@pytest.fixture(scope="module")
def packaged_source_command(tmp_path_factory: pytest.TempPathFactory) -> list[str | Path]:
    root = tmp_path_factory.mktemp("source-intake-kit")
    python = [sys.executable, "-I", "-S", "-B"]
    archive = root / "kit.zip"
    builder = RUNTIME.parents[1] / "tools/build_slack_pilot_kit.py"
    subprocess.run([*python, builder, archive], check=True, capture_output=True, timeout=30)
    with ZipFile(archive) as kit:
        executable = kit.extract("opentour-slack.pyz", root)
    return [*python, executable]


def packaged_mcp_result(command: list[str | Path], name: str, arguments: Json) -> Json:
    frames = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "generated-source-fixture", "version": "1"},
            },
        },
        {"method": "notifications/initialized"},
        {
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ]
    stdin = "".join(canonical({"jsonrpc": "2.0", **frame}) + "\n" for frame in frames)
    output = subprocess.run(
        command, input=stdin, env={}, capture_output=True, text=True, timeout=30, check=True
    )
    assert not output.stderr
    initial, reply = (json.loads(line) for line in output.stdout.splitlines())
    assert initial["id"] == 1 and initial["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert reply["id"] == 2
    return reply["result"]


def test_packaged_merchant_discovery_quarantines_ambiguous_transaction(
    tmp_path: Path, packaged_source_command: list[str | Path]
) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/merchant-discovery-v1.json").read_bytes()
    )
    assert fixture["schema_version"] == "opentour.generated.merchant-discovery.v1"
    expected = fixture["expected"]
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    grant = fixture["grant"]
    store = LocalTrainingStore(private / "training.sqlite3", grant["organization"])
    grant["engagement"] = store.engagement_id
    scope = {
        k: grant[k]
        for k in ("organization", "workflow", "field", "policy_version", "source_schema_version")
    }
    traces = private / "traces.jsonl"
    corrections = private / "corrections.jsonl"
    grant["trace_sha256"] = export(traces, [scope | row for row in fixture["traces"]])
    grant["correction_sha256"] = export(
        corrections, [scope | row for row in fixture["corrections"]]
    )
    row_count = len(fixture["traces"]) + len(fixture["corrections"])
    ledger = LearningMandates(
        private, store.organization, store.engagement_id, owner=grant["owner_id"]
    )
    mandate = json.loads((EXAMPLES / "pilot-learning-mandate.template.json").read_bytes())
    source_scope = {k: grant[k] for k in ("workflow", "field", "trace_source", "correction_source")}
    mandate.update(
        **source_scope,
        mandate_id="generated-merchant-mandate",
        owner_id=grant["owner_id"],
        delegate="generated-worker",
        trainer="generated-worker",
        connectors=["generated-local-source"],
        fields=grant["permitted_features"],
        model_fields=grant["permitted_features"],
        labels=["merchant-a", "merchant-b"],
        permitted_error_pairs=[["merchant-a", "merchant-b"]],
        agreement_revision=digest([grant["policy_version"], grant["retention_terms"]]),
        expires_at=int(time.time()) + 600,
        max_runs=1,
        max_rows_per_run=row_count,
        max_total_rows=row_count,
    )
    enrolled = ledger.enroll(mandate, actor=grant["owner_id"])
    delegate = mandate["delegate"]
    source_receipt = ledger.authorize_source(
        enrolled["mandate_id"],
        {
            **source_scope,
            "connector": mandate["connectors"][0],
            "fields": grant["permitted_features"],
            "rows": row_count,
            "source_revision": digest([grant["trace_sha256"], grant["correction_sha256"]]),
            "trace_sha256": grant["trace_sha256"],
            "correction_sha256": grant["correction_sha256"],
        },
        actor=delegate,
    )
    grant.update(
        expires_at=mandate["expires_at"],
        training_attestation=source_receipt["training_attestation"],
        training_expires_at=mandate["expires_at"],
        delegation={
            "authority_kind": source_receipt["authority_kind"],
            "run_id": source_receipt["run_id"],
            "delegate": delegate,
        },
    )
    grant_path = private / f"cycle-source-{source_receipt['run_id']}.json"
    write(grant_path, canonical(grant))
    request = {
        "pair_ref": "generated-merchant-pair",
        "source_revision": grant["trace_sha256"],
        "correction_revision": grant["correction_sha256"],
        "policy_revision": source_receipt["agreement_revision"],
    }
    selection_path = private / "selection.json"
    write(
        selection_path,
        canonical(
            {
                **request,
                "schema_version": SELECTION_VERSION,
                "tenant": store.organization,
                "actor": delegate,
                "engagement_epoch": store.engagement_id,
                "mandate_id": enrolled["mandate_id"],
                "mandate_revision": enrolled["mandate_revision"],
                "source_run_id": source_receipt["run_id"],
                "workflow_ref": grant["workflow"],
                "trace_source": grant["trace_source"],
                "correction_source": grant["correction_source"],
            }
        ),
    )
    command = [*packaged_source_command, "mcp-intake", selection_path]
    catalog = packaged_mcp_result(command, DISCOVER, {})
    assert catalog["structuredContent"] == {
        "next_cursor": None,
        "sources": [
            {
                "source_kind": "historical",
                "source_ref": request["pair_ref"],
                "source_revision": request["source_revision"],
            }
        ],
    }
    admission = packaged_mcp_result(command, SUBMIT, request | {"idempotency_key": "first"})
    receipt = admission["structuredContent"]
    assert set(receipt) == {"run_ref", "state", "correlation_ref"} and receipt["state"] == "queued"
    status_request = {"run_ref": receipt["run_ref"]}
    initial_status = packaged_mcp_result(command, GET_RUN, status_request)["structuredContent"]
    denied = packaged_mcp_result(
        command,
        SUBMIT,
        request | {"idempotency_key": "first", "correction_revision": digest("different-file")},
    )
    assert denied["isError"] and denied["content"][0]["text"] == "intake_unavailable"
    frozen_path = private / "intake.json"
    assert not frozen_path.exists()
    queue_path = private / "intake.sqlite3"
    with sqlite3.connect(queue_path) as db:
        assert db.execute("SELECT COUNT(*) FROM intake_runs").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM intake_keys").fetchone() == (1,)

    # The queue and source command share the grant and raw-file digests. The test runs
    # source arbitration explicitly because the packaged intake command only queues.

    base = [
        *packaged_source_command,
        "source",
        "--store",
        store.path,
        "--organization",
        store.organization,
    ]
    inputs = ["--grant", grant_path, "--traces", traces, "--corrections", corrections]

    def run(*args: str | Path) -> Json:
        return json.loads(subprocess.check_output([*base, *args], env={}, timeout=30))

    discovery = run("discover", *inputs)
    frozen = run("freeze", *inputs, "--output", frozen_path)
    assert frozen == run("inspect", "--intake", frozen_path)
    assert discovery["suspected_error_classes"] == []
    assert frozen["confirmed_recurring_error"] is False
    case_ids = {row["event_id"] for row in fixture["traces"]}
    for result in (discovery, frozen):
        assert result["development_count"] == 1
        assert result["protected_count"] == expected["protected_count"]
        assert result["excluded_counts"] == {"ambiguous_outcomes": len(expected["excluded"])}
        assert len(case_ids) == (
            result["development_count"]
            + result["protected_count"]
            + sum(result["excluded_counts"].values())
        )
    data = json.loads(frozen_path.read_bytes())
    assert data["excluded"] == expected["excluded"]
    assert data["authority_revision"] == digest(grant)
    assert data["delegation"] == grant["delegation"]
    [row] = data["development"]
    assert row["case_id"] == expected["eligible_case"]
    assert row["outcome_id"] == expected["verified_outcome"]
    assert row["incumbent"]["label"] == "merchant-a" and row["reference"] == "merchant-b"
    assert row["decision_at"] < row["outcome_at"] <= row["verified_at"]
    assert data["training_allowed"] and not data["training_case_ids"]
    with sqlite3.connect(private / "workflow-benchmarks.sqlite3") as db:
        [draft] = db.execute(
            "SELECT tenant, epoch, workflow, run, reference, version, parent, state, materialized "
            "FROM benchmarks"
        ).fetchall()
        assert draft[:4] == (
            store.organization,
            store.engagement_id,
            grant["workflow"],
            receipt["run_ref"],
        )
        assert draft[5:] == (1, None, "draft", None)
        assert db.execute("SELECT COUNT(*) FROM benchmark_operations").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM benchmark_receipts").fetchone() == (0,)
    restarted = packaged_mcp_result(command, GET_RUN, status_request)
    assert restarted["structuredContent"] == {
        "run_ref": receipt["run_ref"],
        "state": "queued",
        "next_action": "wait_for_worker",
        "reason_code": None,
        "workflow_ref": grant["workflow"],
        "benchmark_ref": draft[4],
        "version": 1,
        "benchmark_state": "draft",
    }
    assert restarted["structuredContent"] == initial_status
    replay = packaged_mcp_result(command, SUBMIT, request | {"idempotency_key": "restarted"})
    assert replay["structuredContent"] == receipt
    assert (
        packaged_mcp_result(command, GET_RUN, status_request)["structuredContent"] == initial_status
    )
    with sqlite3.connect(queue_path) as db:
        [run] = db.execute(
            "SELECT tenant, run_ref, workflow_ref, correlation_ref, request, grant_revision "
            "FROM intake_runs"
        ).fetchall()
        assert run[:4] == (
            store.organization,
            receipt["run_ref"],
            grant["workflow"],
            receipt["correlation_ref"],
        )
        assert json.loads(run[4]) == (request | {"mode": "historical"})
        assert run[5] == enrolled["mandate_revision"]
        assert db.execute("SELECT COUNT(*) FROM intake_keys").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM intake_workflows").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM intake_materialization").fetchone() == (0,)
    usage = ledger.inspect(enrolled["mandate_id"], actor=delegate)
    assert (usage["runs"], usage["rows"], usage["reserved_fits"], usage["reserved_seconds"]) == (
        1,
        row_count,
        0,
        0,
    )
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == expected["fit_jobs"]
        assert db.execute("SELECT COUNT(*) FROM fit_checkpoints").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_packaged_invoice_coding_routes_missing_payer_to_review(
    tmp_path: Path, packaged_source_command: list[str | Path]
) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures/payer-routing-v1.json").read_bytes())
    assert fixture["schema_version"] == "opentour.generated.payer-routing.v1"
    expected = fixture["expected"]
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    grant = json.loads((EXAMPLES / "pilot-source-grant.template.json").read_bytes())
    store = LocalTrainingStore(owner / "training.sqlite3", grant["organization"])
    grant.update(
        engagement=store.engagement_id,
        permitted_features=["payer_identity", "batch"],
        training_attestation="att-2222222222222222",
    )
    scope = {
        k: grant[k]
        for k in ("organization", "workflow", "field", "policy_version", "source_schema_version")
    }
    traces, corrections = [], []
    for index, (payer, reference, decision_at) in enumerate(fixture["invoices"]):
        event_id = f"invoice-{index:02}"
        features = {"batch": f"batch-{index:02}"}
        # A missing payer on an otherwise authorized row is eligible for the review fallback.
        if payer is not None:
            features["payer_identity"] = payer
        features.update(fixture["additional_features"].get(event_id, {}))
        available_at = decision_at + (1 if event_id == fixture["late_payer"] else -1)
        traces.append(
            dict(
                scope,
                event_id=event_id,
                source=grant["trace_source"],
                customer_id="generated-client",
                group_id=f"group-{index:02}",
                decision_at=decision_at,
                features_available_at=available_at,
                features=features,
                incumbent={"status": "automatic", "label": "payable"},
            )
        )
        corrections.append(
            dict(
                scope,
                event_id=event_id,
                outcome_id=f"outcome-{index:02}",
                source=grant["correction_source"],
                adjudicator_id=grant["adjudicators"][0],
                facts_as_of=decision_at,
                outcome_at=decision_at + 2,
                verified_at=decision_at + 3,
                reference=reference,
            )
        )
    trace_path, correction_path = owner / "traces.jsonl", owner / "corrections.jsonl"
    grant["trace_sha256"] = export(trace_path, traces)
    grant["correction_sha256"] = export(correction_path, corrections)
    grant_path = owner / "source-grant.json"
    write(grant_path, canonical(grant))

    def run(*args: str | Path) -> str:
        output = subprocess.run(
            [*packaged_source_command, *args],
            env={},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert output.returncode == 0, output.stderr
        assert not output.stderr
        return output.stdout

    source = ["source", "--store", store.path, "--organization", store.organization]
    inputs = ["--grant", grant_path, "--traces", trace_path, "--corrections", correction_path]
    discovered = json.loads(run(*source, "discover", *inputs))
    [error_class] = discovered["suspected_error_classes"]
    assert error_class["count"] == error_class["groups"] == expected["recurring_groups"]
    confirmation = owner / "confirmation.json"
    write(
        confirmation,
        canonical(
            {
                "discovery_revision": discovered["discovery_revision"],
                "class_id": error_class["class_id"],
                "owner_id": grant["owner_id"],
                "attestation": "att-3333333333333333",
            }
        ),
    )
    frozen_path = owner / "intake.json"
    frozen = json.loads(
        run(*source, "freeze", *inputs, "--output", frozen_path, "--confirmation", confirmation)
    )
    assert frozen == json.loads(run(*source, "inspect", "--intake", frozen_path))
    assert frozen["development_count"] == expected["development"]
    assert frozen["protected_count"] == expected["protected"]
    assert frozen["excluded_counts"] == expected["excluded_counts"]
    assert len(traces) == (
        frozen["development_count"]
        + frozen["protected_count"]
        + sum(frozen["excluded_counts"].values())
    )
    data = json.loads(frozen_path.read_bytes())
    assert data["excluded"] == expected["excluded"]
    assert set(data["allowed_features"]) == {"batch", "payer_identity"}
    assert frozen["confirmed_recurring_error"] is True

    manifest = owner / "plan.json"
    write(
        manifest,
        canonical(
            {
                "schema_version": "opentour.pilot.managed-repair.v1",
                "organization": store.organization,
                "engagement": store.engagement_id,
                "intake_file": frozen_path.name,
                "intake_revision": frozen["intake_revision"],
                "incumbent_revision": digest(["always", "payable"]),
                "audience": "generated-reviewer",
                "terms": {"minimum_coverage_bps": 10000, "error_ceiling_bps": 2500},
                "rule": {"feature": "payer_identity", "min_support": 2, "fallback": "review"},
            }
        ),
    )
    args = [
        "--directory",
        owner,
        "--organization",
        store.organization,
        "--manifest",
        manifest,
        "--authorization",
        owner / "authorization.json",
    ]
    revision = json.loads(run("workflow-inspect", *args))["plan_revision"]
    run(
        "workflow-authorize",
        *args,
        "--plan-revision",
        revision,
        "--custodian-attestation",
        "att-5555555555555555",
        "--expires-at",
        str(int(time.time()) + 600),
    )

    def call(name: str, arguments: Json) -> Json:
        result = packaged_mcp_result(
            [*packaged_source_command, "mcp-workflow", *args], name, arguments
        )
        assert not result.get("isError")
        return result["structuredContent"]

    queued = call(
        "start_offline_evaluation", {"plan_revision": revision, "idempotency_ref": "payer-v1"}
    )
    assert queued["state"] == "queued"
    worker = json.loads(run("workflow-worker", *args, "--once"))
    assert worker == call("get_offline_evaluation", {"run_id": queued["run_id"]})
    assert worker["state"] == "held" and worker["reason"] == "evaluation_held"
    result_dir = owner / "managed-outputs" / worker["run_id"]
    rule = json.loads((result_dir / "rule.json").read_bytes())
    report = json.loads((result_dir / "comparison.json").read_bytes())
    assert worker["result"] == {
        "outcome": "hold",
        "case_count": expected["protected"],
        "result_revision": digest(report),
    }
    assert rule["source"] == "development_lookup"
    assert rule["mapping"] == expected["mapping"]
    assert rule["fallback"] == "review"
    assert set(report["arms"]) == {"incumbent", "simple"}
    assert report["arms"]["incumbent"]["metrics"]["incorrect_automatic"] == expected["errors"]
    assert report["arms"]["simple"]["metrics"]["incorrect_automatic"] == 0
    assert report["decision"]["winner"] is None
    assert report["arms"]["simple"]["hold_reason"] == "operating_point_missed_or_undetermined"
    decisions = {row["case_id"]: row["simple"] for row in report["paired_cases"]}
    assert decisions == expected["protected_decisions"]
    assert not decisions.keys() & data["excluded"].keys()
    assert {path.name for path in result_dir.iterdir()} == {
        "comparison.json",
        "plan.json",
        "rule.json",
        "run.json",
    }
    before_replay = {path.name: path.read_bytes() for path in result_dir.iterdir()}
    # Each call starts the shipped legacy managed MCP in a fresh process. Both
    # retry keys must retain the completed result without entering the new intake queue.
    for key in ("payer-v1", "payer-v1-restarted"):
        replay = call(
            "start_offline_evaluation", {"plan_revision": revision, "idempotency_ref": key}
        )
        assert replay == worker
        assert replay["run_id"] == queued["run_id"]
        assert call("get_offline_evaluation", {"run_id": replay["run_id"]}) == worker
    assert json.loads(run("workflow-worker", *args, "--once")) == {"state": "idle"}
    assert {path.name: path.read_bytes() for path in result_dir.iterdir()} == before_replay
    with sqlite3.connect(owner / "managed-runs.sqlite3") as db:
        assert db.execute("SELECT id, attempts FROM runs").fetchall() == [(queued["run_id"], 1)]
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone() == (2,)
    assert not (owner / "intake.sqlite3").exists()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == expected["fit_jobs"]
        assert db.execute("SELECT COUNT(*) FROM fit_checkpoints").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
