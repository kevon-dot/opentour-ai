"""C05 source contracts. Generated mutations never assert product acceptance."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from tools import schema_validate
from tools.validate_spec import ROOT, TARGET, validate_manifest


def manifest():
    return schema_validate.read_json(ROOT / TARGET)


def failures(value):
    return [c for c in validate_manifest(value) if c.status == "FAIL"]


def test_complete_current_manifest_passes_with_actual_native_ids():
    value = manifest()
    checks = validate_manifest(value)
    assert len(checks) > 1000 and not failures(value)
    source = value["source_projection"]
    assert len(source["issues"]) == 294
    assert len(source["features"]) == 32
    assert len({s for f in source["features"] for s in f["implementation_subfeature_ids"]}) == 173
    assert len(source["actions"]) == 175
    assert value["product_acceptance"] == "NOT_RUN"
    assert all(i["native_uuid"] and i["logical_key"] for i in source["issues"])


def test_ac01_exact_unmapped_subfeature_is_reported():
    value = manifest()
    missing = "PRD-17.08"
    for mapping in value["mappings"]:
        mapping["subfeature_ids"] = [x for x in mapping["subfeature_ids"] if x != missing]
    assert any(c.code == "UNMAPPED_SUBFEATURE" and c.subject == missing for c in failures(value))


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("body", "SOURCE_BODY_HASH"),
        ("duplicate", "UNIQUE_STABLE_ID"),
        ("cycle", "ACYCLIC_BLOCKED_BY"),
        ("ownership", "SINGLE_EXCLUSIVE_OWNER"),
        ("unknown_issue", "MAPPED_NATIVE_ISSUE"),
        ("source", "APPROVED_SOURCE_PROJECTION"),
        ("required", "EVERY_ACCEPTANCE_HAS_EVIDENCE"),
        ("action", "UNTESTED_ACTION_REQUIREMENT"),
        ("orphan", "ACTION_OWNER_AND_ASSERTION_REQUIREMENT"),
        ("snapshot", "CURRENT_DESIGN_SNAPSHOT"),
        ("local_hash", "LOCAL_ASSET_SOURCE_HASH"),
        ("missing_asset", "REQUIRED_SOURCE_ASSET"),
        ("contract_ref", "EXACT_DECLARED_CONTRACT_REFERENCES"),
        ("missing_binding", "REQUIRED_TEST_BINDING"),
        ("duplicate_binding", "UNIQUE_TEST_BINDINGS"),
    ],
)
def test_required_adverse_metadata_fixtures(mutation, code):
    value = manifest()
    if mutation == "body":
        value["source_projection"]["issues"][0]["projection_sha256"] = "0" * 64
    elif mutation == "duplicate":
        value["source_projection"]["issues"][1]["logical_key"] = "OT-BOOT"
    elif mutation == "cycle":
        value["source_projection"]["issues"][0]["projection"]["blocked_by"] = ["OPE-6"]
        value["source_projection"]["issues"][1]["projection"]["blocked_by"] = ["OPE-5"]
    elif mutation == "ownership":
        value["exclusive_ownership"].append(copy.deepcopy(value["exclusive_ownership"][0]))
    elif mutation == "unknown_issue":
        value["mappings"][0]["issue_key"] = "OPE-999999"
    elif mutation == "source":
        value["source_projection"]["issues"][0]["technical_capture_sha256"] = "0" * 64
    elif mutation == "required":
        value["mappings"][0]["real_evidence_requirements"] = []
    elif mutation == "action":
        value["action_mappings"].pop()
    elif mutation == "orphan":
        value["action_mappings"][0]["action_id"] = "UI-UNKNOWN-A01"
    elif mutation == "snapshot":
        value["source_bindings"]["figma_revision"] = "unreviewed-revision"
    elif mutation == "local_hash":
        value["local_sources"][0]["sha256"] = "0" * 64
    elif mutation == "missing_asset":
        value["diagram_assets"] = []
    elif mutation == "contract_ref":
        value["mappings"][0]["declared_contract_paths"] = []
    elif mutation == "missing_binding":
        value["test_bindings"].pop()
    elif mutation == "duplicate_binding":
        value["test_bindings"].append(copy.deepcopy(value["test_bindings"][0]))
    assert any(c.code == code for c in failures(value))


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.invalid/schema404",
        "urn:opentour:missing:1.0",
        "urn:opentour:common-contract:1.0#/$defs/nonexistent",
    ],
)
def test_unknown_and_nested_schema_references_fail_offline(ref, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("Network was attempted"))
    value = manifest()
    value["mappings"][0]["contract_refs"] = [ref]
    assert any(c.code == "OFFLINE_SCHEMA_REFERENCE" for c in failures(value))


def test_real_cli_emits_bound_machine_and_readable_findings(tmp_path):
    report, readable = tmp_path / "report.json", tmp_path / "findings.txt"
    env = {
        **os.environ,
        "OPENTOUR_RESULT_NONCE": "a" * 32,
        "OPENTOUR_REPOSITORY_ROOT": str(ROOT),
        "OPENTOUR_TEST_TARGET": TARGET,
        "OPENTOUR_RESULT_FILE": str(report),
    }
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.validate_spec",
            "--result-file",
            str(report),
            "--findings-file",
            str(readable),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(report.read_text())
    schema = schema_validate.read_json(ROOT / "docs/specification/traceability-report.schema.json")
    Draft202012Validator(schema).validate(result)
    assert result["collected"] == result["passed"] == len(result["checks"])
    assert len({c["id"] for c in result["checks"]}) == result["collected"]
    assert result["spec_sha256"] == hashlib.sha256((ROOT / TARGET).read_bytes()).hexdigest()
    assert result["binding_nonce"] == "a" * 32 and result["product_acceptance"] == "NOT_RUN"
    assert "product acceptance: NOT_RUN" in readable.read_text()
    assert report.stat().st_size < 4_000_000
    assert not list(tmp_path.glob("*.writing"))


def test_cli_refuses_existing_report_without_overwriting(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("sentinel")
    env = {
        **os.environ,
        "OPENTOUR_RESULT_NONCE": "a" * 32,
        "OPENTOUR_REPOSITORY_ROOT": str(ROOT),
        "OPENTOUR_TEST_TARGET": TARGET,
        "OPENTOUR_RESULT_FILE": str(report),
    }
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.validate_spec",
            "--result-file",
            str(report),
            "--findings-file",
            str(tmp_path / "findings.txt"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert run.returncode == 2 and report.read_text() == "sentinel"


def test_all_current_c05_cases_have_explicit_requirement_bindings(tmp_path):
    # tools/tests also contains other issues' suites; only C05 owns these files.
    owned_files = (
        "tests/contract/test_traceability.py",
        "tools/tests/test_traceability.py",
        "tools/tests/test_evidence_classification.py",
    )

    def collect(root):
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *owned_files],
            cwd=root,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        return {s for s in run.stdout.splitlines() if "::test_" in s}

    collected = collect(ROOT)
    current = manifest()
    bindings = [b for b in current["test_bindings"] if b["issue_key"] == "OPE-126"]
    assert collected == {b["test_id"] for b in bindings}
    assert {r for b in bindings for r in b["requirement_ids"]} == {
        "OT-BOOT-C05-AC01",
        "OT-BOOT-C05-AC02",
        "OT-BOOT-C05-AC03",
    }

    # Exercise collection with an unrelated CI module that must never be imported.
    for relative in owned_files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_owned(): pass\n")
    (tmp_path / "tools/__init__.py").touch()
    (tmp_path / "tools/tests/__init__.py").touch()
    unrelated = tmp_path / "tools/tests/test_ci_policy.py"
    unrelated.write_text("raise AssertionError('unrelated CI suite was collected')\n")
    assert collect(tmp_path) == {f"{relative}::test_owned" for relative in owned_files}
