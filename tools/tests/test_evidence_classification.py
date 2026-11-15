"""Generated policy/collector fixtures are simulated, including provider-shaped negatives."""

from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tools import schema_validate
from tools.evidence_manifest import (
    EvidenceError,
    EvidenceStore,
    eligibility,
    gate_run,
    parent_completion,
    validate_record,
)
from tools.validate_spec import ROOT, TARGET, digest


def record(kind: str = "simulated", mode: str = "customer_controlled") -> dict[str, Any]:
    return {
        "requirement_ids": ["SENTINEL-AC01", "SENTINEL-SECURITY"],
        "issue_key": "OPE-126",
        "test_id": "test-sentinel",
        "commit_sha": "a" * 40,
        "spec_sha256": "b" * 64,
        "fixture_id": "simulated:traceability-policy",
        "fixture_version": "1",
        "kind": kind,
        "mode": mode,
        "command": ["test-python", "tests/sentinel.py"],
        "started_at": "2026-11-15T00:00:00Z",
        "finished_at": "2026-11-15T00:00:01.000Z",
        "exit_code": 0,
        "status": "PASS",
        "output_digests": {"result": "c" * 64},
        "limitations": ["Generated simulated policy fixture; not actual provider evidence"],
    }


def policy(**changes: Any) -> dict[str, Any]:
    return {
        "commit": "a" * 40,
        "spec": "b" * 64,
        "required_kind": "simulated",
        "required_requirements": {"SENTINEL-AC01", "SENTINEL-SECURITY"},
        "test_id": "test-sentinel",
        "mode": "customer_controlled",
        **changes,
    }


def test_imported_pass_is_never_observed_authority(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "history")
    value = record()
    store.import_history("first", value)
    result = store.eligible("first", **policy())
    assert result["status"] == "BLOCKED" and "UNVERIFIED_PRODUCER" in result["reasons"]
    assert store.read("first")["record"] == value  # History is retained, not silently rewritten.


@pytest.mark.parametrize(
    "kind", ["simulated", "local_integration", "provider_integration", "customer_evidence"]
)
def test_self_claimed_kind_or_runtime_receipt_cannot_authenticate(
    kind: str, tmp_path: Path
) -> None:
    value = record(kind)
    store = EvidenceStore(tmp_path / "history")
    store.import_history("forged", value)
    assert not store.eligible("forged", **policy(required_kind=kind))["eligible"]
    forged = {**value, "trusted_signature": True}
    with pytest.raises(EvidenceError):
        store.import_history("signature-flag", forged)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"commit": "d" * 40}, "STALE_TESTED_COMMIT"),
        ({"spec": "e" * 64}, "CHANGED_REQUIREMENT_SOURCE"),
        ({"required_kind": "provider_integration"}, "REQUIRED_EVIDENCE_KIND_MISMATCH"),
        (
            {"required_requirements": {"MISSING-CRITICAL"}},
            "MISSING_REQUIRED_SECURITY_OR_ACCEPTANCE_CHECK",
        ),
        ({"mode": "opentour_managed"}, "TEST_OR_MODE_MISMATCH"),
    ],
)
def test_ac03_current_readiness_invalidation_preserves_history(
    tmp_path: Path, changes: Any, reason: str
) -> None:
    store = EvidenceStore(tmp_path / "history")
    path = store.import_history("historical", record())
    before = path.read_bytes()
    assert reason in store.eligible("historical", **policy(**changes))["reasons"]
    assert before == path.read_bytes()


@pytest.mark.parametrize("gate", ["G1", "G2", "G3", "G4", "G5", "G6"])
@pytest.mark.parametrize("kind", ["simulated", "local_integration"])
def test_unit_or_local_fake_cannot_close_any_empirical_gate(gate: str, kind: str) -> None:
    values = [record(kind, mode) for mode in ("customer_controlled", "opentour_managed")]
    result = gate_run(
        gate,
        values,
        commit="a" * 40,
        spec="b" * 64,
        required_checks={"SENTINEL-AC01", "SENTINEL-SECURITY"},
        customer_mode="customer_controlled",
    )
    assert result["execution_status"] == "INCOMPLETE"
    assert result["product_acceptance"] == "NOT_RUN"
    modes = {v["mode"] for v in result["missing"]}
    assert modes == (
        {"customer_controlled"} if gate == "G6" else {"customer_controlled", "opentour_managed"}
    )


@pytest.mark.parametrize("kind", ["provider_integration", "customer_evidence"])
def test_missing_gpu_or_customer_verifier_records_blocked(kind: str, tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "history")
    store.missing_prerequisite("missing", record(kind))
    result = store.read("missing")["record"]
    assert result["status"] == "BLOCKED" and result["exit_code"] == 3
    assert "UNQUALIFIED_PROVIDER_VERIFIER" in result["limitations"]
    assert not store.eligible("missing", **policy(required_kind=kind))["eligible"]


def test_parent_requires_all_compatible_children_and_actual_integration() -> None:
    expected = {"child-a": "a" * 40, "integration-child": "b" * 40}
    assert parent_completion(expected, {"child-a": "a" * 40}, integration_eligible=True)[
        "missing_or_incompatible_children"
    ] == ["integration-child"]
    assert parent_completion(expected, expected, integration_eligible=False)["status"] == "BLOCKED"
    assert parent_completion(expected, expected, integration_eligible=True)["status"] == "PASS"
    assert parent_completion({}, {}, integration_eligible=True)["status"] == "BLOCKED"


def test_replay_is_idempotent_and_changed_same_attempt_conflicts(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "history")
    first = store.import_history("replay", record())
    assert store.import_history("replay", record()) == first
    changed = record()
    changed["spec_sha256"] = "d" * 64
    with pytest.raises(EvidenceError, match="CONTENT_CONFLICT"):
        store.import_history("replay", changed)
    assert store.read("replay")["record"] == record()


def test_concurrent_identical_publication_is_one_complete_record(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "history")
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: store.import_history("concurrent", record()), range(8)))
    assert len(set(paths)) == 1 and store.read("concurrent")["record"] == record()
    assert [p.name for p in store.directory.iterdir()] == ["concurrent.json"]


def test_interrupted_publication_keeps_old_history_and_no_partial_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceStore(tmp_path / "history")
    store.import_history("old", record())
    import os

    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("sentinel")))
    with pytest.raises(OSError):
        store.import_history("interrupted", record())
    assert store.read("old")["record"] == record()
    assert not (store.directory / "interrupted.json").exists()
    assert not list(store.directory.glob(".pending-*"))


def test_historical_tampering_and_symlink_are_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "history")
    path = store.import_history("tamper", record())
    value = json.loads(path.read_text())
    value["record"]["kind"] = "provider_integration"
    path.write_text(json.dumps(value))
    with pytest.raises(EvidenceError, match="INTEGRITY"):
        store.read("tamper")
    (store.directory / "link.json").symlink_to(path)
    with pytest.raises(EvidenceError):
        store.read("link")


@pytest.mark.parametrize(
    "field,value",
    [
        ("exit_code", True),
        ("exit_code", 3),
        ("status", "INCOMPLETE"),
        ("kind", "real"),
        ("mode", "any"),
        ("commit_sha", "latest"),
        ("started_at", "2026-11-15T01:00:00+01:00"),
        ("finished_at", "2026-11-14T23:59:59Z"),
        ("output_digests", {}),
    ],
)
def test_closed_record_and_separate_status_vocabulary(field: str, value: Any) -> None:
    current = record()
    current[field] = value
    with pytest.raises(EvidenceError):
        validate_record(current)


def test_timestamp_precision_does_not_change_order() -> None:
    value = record()
    value["finished_at"] = "2026-11-15T00:00:00.000Z"
    validate_record(value)


def observation_arguments() -> dict[str, Any]:
    m = schema_validate.read_json(ROOT / TARGET)
    source = next(i for i in m["source_projection"]["issues"] if i["issue_key"] == "OPE-126")
    return {
        "command": "test-python",
        "args": ["tests/contract/test_traceability.py"],
        "root": ROOT,
        "requirement_ids": [source["projection"]["requirement_ids"][0]],
        "issue_key": "OPE-126",
        "test_id": (
            "tests/contract/test_traceability.py::test_ac01_exact_unmapped_subfeature_is_reported"
        ),
        "spec_sha256": source["technical_capture_sha256"],
        "fixture_id": "bootstrap-command-contract",
        "fixture_version": "1",
        "mode": None,
    }


def producer_fixture(args: Any) -> dict[str, Any]:
    return {
        "status": "PASS",
        "exit_code": 0,
        "limitations": ["Simulated producer-boundary test"],
        "fixture_id": "bootstrap-command-contract",
        "fixture_version": "1",
        "steps": [
            {
                "tests": {
                    "tests": [
                        {
                            "id": "sha256:" + hashlib.sha256(args["test_id"].encode()).hexdigest(),
                            "file": args["test_id"].split("::", 1)[0],
                            "mode": None,
                            "status": "passed",
                        }
                    ]
                }
            }
        ],
        "git_worktree_dirty": False,
        "git_head_changed": False,
        "kind": "simulated",
        "commit_sha": "a" * 40,
        "started_at": "2026-11-15T00:00:00Z",
        "finished_at": "2026-11-15T00:00:01Z",
    }


@pytest.mark.parametrize("mutation", ["none", "zero", "skip", "dirty", "provider", "fixture"])
def test_supervised_producer_boundary_and_required_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from tools import test_runner

    args = observation_arguments()
    receipt = producer_fixture(args)
    if mutation == "zero":
        receipt["steps"][0]["tests"]["tests"] = []
    elif mutation == "skip":
        receipt["steps"][0]["tests"]["tests"][0]["status"] = "skipped"
    elif mutation == "dirty":
        receipt["git_worktree_dirty"] = True
    elif mutation == "provider":
        receipt["kind"] = "provider_integration"
    elif mutation == "fixture":
        receipt["fixture_version"] = "2"
    monkeypatch.setattr(test_runner, "run_command", lambda *a, **k: copy.deepcopy(receipt))
    store = EvidenceStore(tmp_path / "history")
    store.observe_command("controlled", **args)
    stored = store.read("controlled")["record"]
    result = store.eligible(
        "controlled",
        commit="a" * 40,
        spec=args["spec_sha256"],
        required_kind="simulated",
        required_requirements=set(args["requirement_ids"]),
        test_id=args["test_id"],
        mode=None,
    )
    assert result["eligible"] is (mutation == "none")
    assert stored["output_digests"]["command_receipt"] == digest(receipt)


def test_arbitrary_requirement_or_test_cannot_be_attached_to_success(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "history")
    args = observation_arguments()
    args["requirement_ids"] = ["UNOWNED-SECURITY"]
    with pytest.raises(EvidenceError, match="UNBOUND_REQUIREMENT"):
        store.observe_command("unowned", **args)


def test_provider_shaped_receipt_and_private_provenance_flag_still_cannot_qualify() -> None:
    value = record("provider_integration")
    receipt = {"status": "PASS", "trusted": True}
    value["output_digests"] = {"command_receipt": digest(receipt)}
    result = eligibility(
        value,
        **policy(required_kind="provider_integration"),
        _provenance={"origin": "observed_command", "receipt": receipt},
    )
    assert "UNVERIFIED_PRODUCER" in result["reasons"]


def test_observed_retry_reuses_original_receipt_without_running_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import test_runner

    args = observation_arguments()
    receipt = producer_fixture(args)
    calls = []

    def execute(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        return copy.deepcopy(receipt)

    monkeypatch.setattr(test_runner, "run_command", execute)
    store = EvidenceStore(tmp_path / "history")
    first = store.observe_command("retry", **args)
    assert store.observe_command("retry", **args) == first
    assert calls == [1]
