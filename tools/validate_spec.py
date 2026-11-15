"""Offline source/issue/test traceability. Metadata conformance is not product acceptance.

The reviewed source projection is deliberately technical metadata, not a copy of
native issue bodies. Its pinned digest binds the selected IDs and source hashes;
local sources additionally verify actual bytes. Refresh requires source review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tools import schema_validate

ROOT = Path(__file__).resolve().parents[1]
TARGET = "docs/specification/manifest.json"
APPROVED_PROJECTION = "fe7a428d5ff650ba843466031d4eb9b622c0759e6baee3eac5774baf2244e948"
REPORT_SCHEMA = "docs/specification/traceability-report.schema.json"


class ConfigurationError(ValueError):
    """Messages are fixed safe codes, never raw submitted data."""


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def closed(value: Any, keys: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ConfigurationError("INVALID_CLOSED_OBJECT")
    return value


def strings(value: Any, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(x, str) or not x or len(x) > 2048 for x in value)
        or len(value) != len(set(value))
        or (nonempty and not value)
    ):
        raise ConfigurationError("INVALID_UNIQUE_STRING_ARRAY")
    return value


def rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ConfigurationError("INVALID_OBJECT_ARRAY")
    return value


def safe_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./:#-]{1,2048}", value):
        return "INVALID_IDENTIFIER"
    return value


@dataclass(frozen=True)
class Check:
    id: str
    owner: str
    status: str
    code: str
    subject: str


class Checks:
    def __init__(self) -> None:
        self.items: list[Check] = []

    def add(self, passed: bool, owner: str, code: str, subject: str) -> None:
        self.items.append(
            Check(
                id=f"traceability:{len(self.items) + 1}",
                owner=safe_id(owner),
                status="PASS" if passed else "FAIL",
                code=code,
                subject=safe_id(subject),
            )
        )


def cycle_path(graph: dict[str, list[str]]) -> list[str]:
    active: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str]:
        if node in active:
            return active[active.index(node) :] + [node]
        if node in done:
            return []
        active.append(node)
        for dependency in graph[node]:
            cycle = visit(dependency)
            if cycle:
                return cycle
        active.pop()
        done.add(node)
        return []

    for node in graph:
        found = visit(node)
        if found:
            return found
    return []


def validate_manifest(
    document: Any,
    root: Path = ROOT,
    *,
    expected_projection: str = APPROVED_PROJECTION,
) -> list[Check]:
    """Validate all required memberships, never a percentage or status shortcut."""
    m = closed(
        document,
        "schema_version source_projection source_projection_sha256 "
        "source_bindings mappings exclusive_ownership ownership_handoffs local_sources "
        "diagram_assets diagram_requirements action_mappings product_acceptance test_bindings",
    )
    if m["schema_version"] != "opentour.traceability-manifest.v1":
        raise ConfigurationError("INVALID_MANIFEST_VERSION")
    c = Checks()
    c.add(m["product_acceptance"] == "NOT_RUN", "OPE-126", "METADATA_ONLY", TARGET)
    source = closed(m["source_projection"], "features issues actions required_assets")
    c.add(
        digest(source) == m["source_projection_sha256"] == expected_projection,
        "OPE-126",
        "APPROVED_SOURCE_PROJECTION",
        TARGET,
    )
    bindings = closed(
        m["source_bindings"],
        "feature_register_sha256 figma_snapshot_sha256 figma_revision native_projection",
    )
    c.add(
        bindings["feature_register_sha256"]
        == "f6d1061a103a028b6309928a1c6d96b1c58cb969793a2b9e8a4e36dd50b4602f",
        "OPE-126",
        "FEATURE_REGISTER_SOURCE",
        "feature-register",
    )
    c.add(
        bindings["figma_snapshot_sha256"]
        == "38c3537dd10719a1978873137d34086fa51e284456ed7c5d46d36e77815367a7"
        and bindings["figma_revision"] == "approved-ui-design-index.v4",
        "OPE-299",
        "CURRENT_DESIGN_SNAPSHOT",
        "figma-snapshot",
    )
    features = rows(source["features"])
    issues = rows(source["issues"])
    actions = rows(source["actions"])
    known_issues: dict[str, dict[str, Any]] = {}
    logical: set[str] = set()
    uuids: set[str] = set()
    graph: dict[str, list[str]] = {}
    requirements: set[str] = set()
    for issue in issues:
        closed(
            issue,
            "issue_key native_uuid logical_key url updated_at publication_spec_sha256 "
            "technical_capture_sha256 projection projection_sha256",
        )
        key = issue["issue_key"]
        if not isinstance(key, str) or not re.fullmatch(r"OPE-[1-9][0-9]*", key):
            raise ConfigurationError("INVALID_NATIVE_ISSUE_ID")
        c.add(
            key not in known_issues
            and issue["logical_key"] not in logical
            and issue["native_uuid"] not in uuids,
            key,
            "UNIQUE_STABLE_ID",
            key,
        )
        logical.add(issue["logical_key"])
        uuids.add(issue["native_uuid"])
        known_issues[key] = issue
        body = closed(
            issue["projection"],
            "requirement_ids requirement_hashes feature_ids "
            "subfeature_ids owned_paths declared_test_paths parent blocked_by "
            "declared_contract_paths referenced_issue_keys",
        )
        c.add(digest(body) == issue["projection_sha256"], key, "SOURCE_BODY_HASH", key)
        source_reqs = strings(body["requirement_ids"], nonempty=True)
        c.add(not (set(source_reqs) & requirements), key, "UNIQUE_REQUIREMENT_IDS", key)
        c.add(
            isinstance(body["requirement_hashes"], dict)
            and set(body["requirement_hashes"]) == set(source_reqs)
            and all(
                isinstance(h, str) and re.fullmatch(r"[a-f0-9]{64}", h)
                for h in body["requirement_hashes"].values()
            ),
            key,
            "ACCEPTANCE_SOURCE_BINDING",
            key,
        )
        requirements.update(source_reqs)
        for field in ("feature_ids", "subfeature_ids", "owned_paths", "declared_test_paths"):
            strings(body[field])
        graph[key] = strings(body["blocked_by"])
    known = set(known_issues)
    valid_graph = all(set(edges) <= known for edges in graph.values())
    c.add(valid_graph, "OPE-126", "DEPENDENCY_IDS_EXIST", "blocked-by")
    if valid_graph:
        cycle = cycle_path(graph)
        c.add(
            not cycle,
            cycle[0] if cycle else "OPE-126",
            "ACYCLIC_BLOCKED_BY",
            "/".join(cycle) if cycle else "blocked-by",
        )
    for key, issue in known_issues.items():
        parent = issue["projection"]["parent"]
        c.add(parent is None or parent in known - {key}, key, "PARENT_REFERENCE", key)
    canonical_features: set[str] = set()
    canonical_subfeatures: set[str] = set()
    for feature in features:
        closed(feature, "id implementation_issue_keys implementation_subfeature_ids diagram")
        c.add(feature["id"] not in canonical_features, "OPE-126", "UNIQUE_FEATURE", feature["id"])
        canonical_features.add(feature["id"])
        canonical_subfeatures.update(
            strings(feature["implementation_subfeature_ids"], nonempty=True)
        )
        c.add(
            set(strings(feature["implementation_issue_keys"], nonempty=True)) <= logical,
            "OPE-126",
            "FEATURE_ISSUES_EXIST",
            feature["id"],
        )
    registry = schema_validate.schema_registry(root)
    mapped_issues: set[str] = set()
    mapped_requirements: set[str] = set()
    mapped_subfeatures: set[str] = set()
    mapped_features: set[str] = set()
    for mapping in rows(m["mappings"]):
        closed(
            mapping,
            "issue_key requirement_ids feature_ids subfeature_ids owned_paths "
            "contract_refs test_ids real_evidence_requirements evidence_outputs "
            "implementation_acceptance declared_contract_paths referenced_issue_keys",
        )
        key = mapping["issue_key"]
        c.add(key in known and key not in mapped_issues, key, "MAPPED_NATIVE_ISSUE", key)
        mapped_issues.add(key)
        reqs = set(strings(mapping["requirement_ids"], nonempty=True))
        subfeatures = set(strings(mapping["subfeature_ids"]))
        feature_ids = set(strings(mapping["feature_ids"]))
        owned = strings(mapping["owned_paths"])
        strings(mapping["test_ids"])
        strings(mapping["evidence_outputs"], nonempty=True)
        mapped_requirements.update(reqs)
        mapped_subfeatures.update(subfeatures)
        mapped_features.update(feature_ids)
        c.add(
            subfeatures <= canonical_subfeatures and feature_ids <= canonical_features,
            key,
            "CANONICAL_FEATURE_IDS",
            key,
        )
        if key in known:
            body = known_issues[key]["projection"]
            c.add(reqs == set(body["requirement_ids"]), key, "EXACT_ISSUE_ACCEPTANCE", key)
            c.add(set(owned) == set(body["owned_paths"]), key, "OWNED_PATH_BINDING", key)
            for reference_field in ("declared_contract_paths", "referenced_issue_keys"):
                actual = strings(mapping[reference_field])
                c.add(
                    set(actual) == set(strings(body[reference_field])),
                    key,
                    "EXACT_DECLARED_CONTRACT_REFERENCES",
                    key,
                )
            c.add(
                set(mapping["referenced_issue_keys"]) <= known,
                key,
                "REFERENCED_PROVIDER_ISSUES_EXIST",
                key,
            )
            for missing in sorted(set(body["subfeature_ids"]) - subfeatures):
                c.add(False, key, "UNMAPPED_SUBFEATURE", missing)
            c.add(
                subfeatures == set(body["subfeature_ids"])
                and feature_ids == set(body["feature_ids"]),
                key,
                "EXACT_ISSUE_FEATURE_BINDING",
                key,
            )
        for ref in strings(mapping["contract_refs"], nonempty=True):
            try:
                registry.resolver().lookup(ref)
                valid = True
            except Exception:
                valid = False
            c.add(valid, key, "OFFLINE_SCHEMA_REFERENCE", ref)
        evidence_ids = set()
        for requirement in rows(mapping["real_evidence_requirements"]):
            closed(requirement, "requirement_id name status")
            ident = requirement["requirement_id"]
            c.add(
                ident in reqs
                and ident not in evidence_ids
                and isinstance(requirement["name"], str)
                and bool(requirement["name"])
                and requirement["status"] == "NOT_RUN",
                key,
                "NAMED_REAL_EVIDENCE",
                ident,
            )
            evidence_ids.add(ident)
        # This metadata projection does not claim that a future test path already exists.
        c.add(evidence_ids == reqs, key, "EVERY_ACCEPTANCE_HAS_EVIDENCE", key)
        c.add(mapping["implementation_acceptance"] == "NOT_RUN", key, "NO_METADATA_ACCEPTANCE", key)
    for missing in sorted(canonical_subfeatures - mapped_subfeatures):
        c.add(False, "OPE-126", "UNMAPPED_SUBFEATURE", missing)
    for missing in sorted(canonical_features - mapped_features):
        c.add(False, "OPE-126", "UNMAPPED_FEATURE", missing)
    for missing in sorted(requirements - mapped_requirements):
        c.add(False, "OPE-126", "UNMAPPED_REQUIREMENT", missing)
    c.add(mapped_requirements <= requirements, "OPE-126", "NO_UNKNOWN_REQUIREMENTS", "requirements")
    c.add(mapped_issues == known, "OPE-126", "EXACT_NATIVE_ISSUE_SET", "issues")
    ownership: dict[str, str] = {}
    for entry in rows(m["exclusive_ownership"]):
        closed(entry, "path owner")
        path, owner = entry["path"], entry["owner"]
        c.add(path not in ownership, owner, "SINGLE_EXCLUSIVE_OWNER", path)
        c.add(
            owner in known and path in known_issues[owner]["projection"]["owned_paths"],
            owner,
            "OWNERSHIP_SOURCE_BINDING",
            path,
        )
        ownership[path] = owner
    expected_owned = {
        (path, key)
        for key, issue in known_issues.items()
        for path in issue["projection"]["owned_paths"]
    }
    c.add(set(ownership.items()) == expected_owned, "OPE-126", "EXACT_OWNED_PATH_SET", "ownership")
    for handoff in rows(m["ownership_handoffs"]):
        closed(handoff, "path from to accepted_commit")
        c.add(
            handoff["from"] in known
            and ownership.get(handoff["path"]) == handoff["to"]
            and bool(re.fullmatch(r"[a-f0-9]{7,40}", handoff["accepted_commit"])),
            handoff["to"],
            "SERIALIZED_OWNERSHIP_HANDOFF",
            handoff["path"],
        )
    supplied_assets = {digest(a) for a in rows(m["local_sources"]) + rows(m["diagram_assets"])}
    for asset in rows(source["required_assets"]):
        closed(asset, "path sha256 schema_id owner")
        c.add(
            digest(asset) in supplied_assets, asset["owner"], "REQUIRED_SOURCE_ASSET", asset["path"]
        )
    for field in ("local_sources", "diagram_assets"):
        for asset in rows(m[field]):
            closed(asset, "path sha256 schema_id owner")
            try:
                path = schema_validate.local_file(root, asset["path"])
                matches = hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]
                if asset["schema_id"] is not None:
                    matches = (
                        matches and schema_validate.read_json(path).get("$id") == asset["schema_id"]
                    )
                    registry.resolver().lookup(asset["schema_id"])
            except (schema_validate.SchemaError, OSError, ValueError, AttributeError):
                matches = False
            c.add(matches, asset["owner"], "LOCAL_ASSET_SOURCE_HASH", asset["path"])
    diagrams = rows(m["diagram_requirements"])
    for item in diagrams:
        closed(item, "feature_id name status owner")
        c.add(
            item["feature_id"] in canonical_features
            and item["owner"] in known
            and item["status"] == "NOT_RUN"
            and bool(item["name"]),
            item["owner"],
            "DECLARED_DIAGRAM_REQUIREMENT",
            item["feature_id"],
        )
    c.add(
        {x["feature_id"] for x in diagrams} == canonical_features,
        "OPE-126",
        "ALL_DIAGRAM_REQUIREMENTS",
        "diagrams",
    )
    action_ids: dict[str, str] = {}
    for action in actions:
        closed(action, "action_id owner figma_node_id required_states")
        strings(action["required_states"])
        c.add(
            action["action_id"] not in action_ids
            and action["owner"] in known
            and bool(re.fullmatch(r"[0-9]+:[0-9]+", action["figma_node_id"])),
            action["owner"],
            "CANONICAL_ACTION_ID",
            action["action_id"],
        )
        action_ids[action["action_id"]] = action["owner"]
    mapped_actions: set[str] = set()
    for action in rows(m["action_mappings"]):
        closed(action, "action_id owner evidence_requirement status")
        ident = action["action_id"]
        c.add(
            ident not in mapped_actions
            and action_ids.get(ident) == action["owner"]
            and bool(action["evidence_requirement"])
            and action["status"] == "NOT_RUN",
            action["owner"],
            "ACTION_OWNER_AND_ASSERTION_REQUIREMENT",
            ident,
        )
        mapped_actions.add(ident)
    for ident in sorted(set(action_ids) - mapped_actions):
        c.add(False, action_ids[ident], "UNTESTED_ACTION_REQUIREMENT", ident)
    c.add(mapped_actions <= set(action_ids), "OPE-299", "NO_ORPHAN_ACTIONS", "actions")
    for binding in rows(m["test_bindings"]):
        closed(binding, "issue_key requirement_ids test_id kind mode fixture_id fixture_version")
        key = binding["issue_key"]
        binding_reqs = strings(binding["requirement_ids"], nonempty=True)
        test_path = str(binding["test_id"]).split("::", 1)[0]
        c.add(
            key in known
            and set(binding_reqs) <= set(known_issues[key]["projection"]["requirement_ids"])
            and test_path in known_issues[key]["projection"]["declared_test_paths"]
            and binding["kind"] in {"simulated", "local_integration"}
            and binding["mode"] in {None, "customer_controlled", "opentour_managed"},
            key,
            "TEST_REQUIREMENT_BINDING",
            binding["test_id"],
        )
        try:
            schema_validate.local_file(root, test_path)
            present = "::" in binding["test_id"]
        except schema_validate.SchemaError:
            present = False
        c.add(present, key, "REGISTERED_TEST_PATH_EXISTS", binding["test_id"])
    return c.items


def validate_ui_report(
    report: Any,
    *,
    expected_actions: set[str],
    manifest_digest: str,
    provider_schema_digest: str,
    figma_revision: str,
    commit: str,
) -> list[str]:
    """OPE-299 conformance seam only; callers still need an observed/authenticated run."""
    closed(
        report,
        "manifest_digest provider_schema_digest figma_revision checked_commit "
        "expected_actions checked_actions failures status",
    )
    if not expected_actions:
        raise ConfigurationError("EMPTY_REQUIRED_ACTION_SET")
    expected = strings(report["expected_actions"])
    checked = strings(report["checked_actions"])
    reasons = []
    for key, wanted in (
        ("manifest_digest", manifest_digest),
        ("provider_schema_digest", provider_schema_digest),
        ("figma_revision", figma_revision),
        ("checked_commit", commit),
    ):
        if report[key] != wanted:
            reasons.append(f"STALE_{key.upper()}")
    if set(expected) != expected_actions or set(checked) != expected_actions:
        reasons.append("ACTION_SET_MISMATCH")
    for failure in rows(report["failures"]):
        closed(failure, "action_id owner reason")
        if not all(isinstance(v, str) and v for v in failure.values()):
            raise ConfigurationError("INVALID_ACTION_FAILURE")
    if report["failures"] or report["status"] != "PASS":
        reasons.append("ACTION_ASSERTIONS_FAILED")
    return reasons


def compare_action_binding(
    binding: dict[str, Any],
    provider: dict[str, Any],
    *,
    parameter_mapping: dict[str, str],
    required_assertions: set[str],
    observed_assertions: set[str],
) -> list[str]:
    """Compare an owning collector's projected operation, without inventing an API.

    Provider is a trusted, separately source-bound projection. This comparison is
    not a provider schema registry or an implementation of the future UI collector.
    """
    fields = (
        "method path_template plane capability request_fields "
        "request_schema_ref response_schema_refs"
    )
    closed(binding, fields)
    closed(provider, fields)
    for operation in (binding, provider):
        strings(operation["request_fields"])
        if (
            any(
                not isinstance(operation[k], str) or not operation[k]
                for k in ("method", "path_template", "plane", "capability", "request_schema_ref")
            )
            or not isinstance(operation["response_schema_refs"], dict)
            or not operation["response_schema_refs"]
            or any(
                not isinstance(k, str) or not isinstance(v, str) or not v
                for k, v in operation["response_schema_refs"].items()
            )
            or not required_assertions
        ):
            raise ConfigurationError("INVALID_OPERATION_PROJECTION")
    reasons = []
    for key in fields.split():
        if key == "path_template":
            continue
        if binding[key] != provider[key]:
            reasons.append(f"PROVIDER_{key.upper()}_DRIFT")
    left = binding["path_template"].split("/")
    right = provider["path_template"].split("/")
    paths_match = len(left) == len(right)
    used: dict[str, str] = {}
    for actual, canonical in zip(left, right, strict=False):
        if actual == canonical:
            continue
        a = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", actual)
        b = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", canonical)
        if not a or not b or parameter_mapping.get(a[1]) != b[1]:
            paths_match = False
        else:
            used[a[1]] = b[1]
    if not paths_match or used != parameter_mapping or len(set(used.values())) != len(used):
        reasons.append("PROVIDER_PATH_DRIFT")
    if not required_assertions <= observed_assertions:
        reasons.append("MISSING_REQUIRED_ADVERSE_ASSERTION")
    return reasons


def report_document(
    checks: list[Check], *, nonce: str, commit: str, spec: str, config_error: bool = False
) -> dict[str, Any]:
    failed = sum(c.status == "FAIL" for c in checks)
    passed = len(checks) - failed
    success = bool(checks) and not failed and not config_error
    return {
        "schema_version": "opentour.traceability-report.v1",
        "runner": "semantic_validator",
        "target": TARGET,
        "binding_nonce": nonce,
        "commit_sha": commit,
        "spec_sha256": spec,
        "kind": "simulated",
        "scope": "bootstrap_metadata_conformance",
        "product_acceptance": "NOT_RUN",
        "checks": [asdict(c) for c in checks],
        "collected": len(checks),
        "passed": passed,
        "failed": failed,
        "status": "PASS" if success else "FAIL",
        "exit_code": 0 if success else 2 if config_error else 1,
        "reason_code": "INVALID_CONFIGURATION"
        if config_error
        else None
        if success
        else "TRACEABILITY_FAILED",
        "cleanup_complete": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fixed offline traceability validator")
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--findings-file", required=True)
    args = parser.parse_args(argv)
    result, readable = Path(args.result_file), Path(args.findings_file)
    nonce = os.environ.get("OPENTOUR_RESULT_NONCE", "")
    if (
        not re.fullmatch(r"[a-f0-9]{32}", nonce)
        or os.environ.get("OPENTOUR_REPOSITORY_ROOT") != str(ROOT)
        or os.environ.get("OPENTOUR_TEST_TARGET") != TARGET
        or os.environ.get("OPENTOUR_RESULT_FILE") != str(result)
        or result == readable
        or result.parent != readable.parent
        or any(
            p.exists() or p.is_symlink() or not p.parent.is_dir() or p.parent.is_symlink()
            for p in (result, readable)
        )
    ):
        return 2
    config_error = False
    spec = "0" * 64
    commit = "0" * 40
    try:
        commit = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ConfigurationError("INVALID_COMMIT")
        manifest_path = schema_validate.local_file(ROOT, TARGET)
        if manifest_path.stat().st_size > 4_000_000:
            raise ConfigurationError("MANIFEST_TOO_LARGE")
        spec = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        checks = validate_manifest(schema_validate.read_json(manifest_path))

    except (
        ConfigurationError,
        schema_validate.SchemaError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        config_error = True
        checks = [
            Check("traceability:configuration", "OPE-126", "FAIL", "INVALID_CONFIGURATION", TARGET)
        ]
    report = report_document(
        checks, nonce=nonce, commit=commit, spec=spec, config_error=config_error
    )
    lines = [
        "Traceability findings",
        f"Status: {report['status']}",
        "Scope: bootstrap metadata conformance; product acceptance: NOT_RUN",
        f"Checks: {report['collected']}; passed: {report['passed']}; failed: {report['failed']}",
    ]
    lines += [f"{c.owner}: {c.code}: {c.subject}" for c in checks if c.status == "FAIL"]
    if not report["failed"]:
        lines.append(
            "All declared metadata checks passed. Future acceptance evidence remains NOT_RUN."
        )
    # Each file appears only after its complete bounded contents have been flushed.
    for path, content in (
        (readable, "\n".join(lines) + "\n"),
        (result, json.dumps(report, sort_keys=True) + "\n"),
    ):
        temporary = path.with_name(path.name + ".writing")
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
