"""Generated OSV-format adversaries; no network scanner or provider qualification."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools.ci.dependency_evidence import (
    LOCKFILES,
    MAX_REPORT_BYTES,
    DependencyEvidenceError,
    project_osv_report,
)
from tools.public_content_check import inspect_bytes, load_policy

ROOT = Path(__file__).resolve().parents[2]


def fixture() -> dict[str, Any]:
    return {
        "results": [
            {
                "source": {"type": "lockfile", "path": str(ROOT / path)},
                "packages": [
                    {
                        "package": {
                            "name": "generated-package",
                            "version": "1.0.0",
                            "ecosystem": "npm" if path == "pnpm-lock.yaml" else "PyPI",
                            "purl": "pkg:pypi/generated-package@1.0.0",
                        }
                    }
                ],
            }
            for path in sorted(LOCKFILES)
        ],
        "experimental_config": {"licenses": {"summary": False, "allowlist": None}},
    }


def package(data: dict[str, Any]) -> dict[str, Any]:
    return data["results"][0]["packages"][0]  # type: ignore[no-any-return]


def vulnerable() -> dict[str, Any]:
    data = fixture()
    package(data).update(
        {
            "vulnerabilities": [
                {
                    "id": "GHSA-aaaa-bbbb-cccc",
                    "aliases": ["CVE-2026-10001"],
                    "severity": [
                        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}
                    ],
                    "affected": [
                        {
                            "package": {"name": "generated-package", "ecosystem": "PyPI"},
                            "severity": [{"type": "Ubuntu", "score": "high", "source": "SELF"}],
                            "ranges": [
                                {
                                    "type": "SEMVER",
                                    "events": [{"introduced": "0"}, {"fixed": "1.0.1"}],
                                }
                            ],
                        }
                    ],
                }
            ],
            "groups": [
                {
                    "ids": ["GHSA-aaaa-bbbb-cccc"],
                    "aliases": ["CVE-2026-10001"],
                    "max_severity": "9.8",
                }
            ],
        }
    )
    return data


def project(value: Any) -> dict[str, Any]:
    return project_osv_report(json.dumps(value).encode(), root=ROOT)


def test_clean_three_lock_inventory_retained_with_original_digest() -> None:
    value = fixture()
    raw = json.dumps(value, indent=2).encode()
    result = project_osv_report(raw, root=ROOT)
    assert result["status"] == "PASS" and not result["findings_present"]
    assert result["original_output_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["original_output_bytes"] == len(raw)
    assert result["counts"] == {
        "sources": 3,
        "packages": 3,
        "affected_packages": 0,
        "vulnerability_occurrences": 0,
        "unique_vulnerabilities": 0,
        "groups": 0,
        "license_violations": 0,
        "deprecated_packages": 0,
        "withdrawn_vulnerability_occurrences": 0,
        "uncalled_groups": 0,
        "unimportant_groups": 0,
    }
    assert {item["source"]["path"] for item in result["results"]} == LOCKFILES
    assert str(ROOT) not in json.dumps(result)


def test_relative_sources_are_accepted_without_reading_files() -> None:
    data = fixture()
    for item in data["results"]:
        item["source"]["path"] = str(Path(item["source"]["path"]).relative_to(ROOT))
    assert project(data)["status"] == "PASS"


def test_all_findings_severity_fixes_and_alias_groups_survive() -> None:
    data = vulnerable()
    first = package(data)
    duplicate = copy.deepcopy(first["vulnerabilities"][0])
    duplicate["id"] = "PYSEC-2026-1"
    first["vulnerabilities"].append(duplicate)
    first["groups"][0]["ids"].append(duplicate["id"])
    data["results"][1]["packages"].append(copy.deepcopy(first))
    result = project(data)
    assert result["status"] == "FAIL" and result["findings_present"]
    assert result["counts"]["vulnerability_occurrences"] == 4
    assert result["counts"]["unique_vulnerabilities"] == 2
    assert result["counts"]["groups"] == 2
    retained = package(result)
    assert retained["groups"][0]["ids"] == first["groups"][0]["ids"]
    assert retained["groups"][0]["max_severity"] == "9.8"
    assert retained["vulnerabilities"][0]["severity"] == first["vulnerabilities"][0]["severity"]
    assert retained["vulnerabilities"][0]["affected"][0]["ranges"][0]["events"][-1] == {
        "fixed": "1.0.1"
    }
    assert retained["vulnerabilities"][0]["affected"][0]["severity"][0]["score"] == "high"


def test_contact_and_descriptive_metadata_removed_without_losing_finding() -> None:
    data = vulnerable()
    # Generated at runtime so repository source itself contains no contact address.
    contact = "generated-maintainer" + "@" + "noncustomer-mail" + ".dev"
    vuln = package(data)["vulnerabilities"][0]
    vuln.update(
        {
            "summary": contact,
            "details": "Contact " + contact,
            "credits": [{"name": "Generated reporter", "contact": [contact], "type": "FINDER"}],
            "references": [{"type": "WEB", "url": "mailto:" + contact}],
            "database_specific": {"maintainers": [contact], "description": contact},
        }
    )
    raw = json.dumps(data).encode()
    assert inspect_bytes("raw.json", raw, load_policy(ROOT))
    result = project_osv_report(raw, root=ROOT)
    assert result["counts"]["vulnerability_occurrences"] == 1
    assert result["status"] == "FAIL"
    assert contact not in json.dumps(result)
    assert not inspect_bytes("projection.json", json.dumps(result).encode(), load_policy(ROOT))


@pytest.mark.parametrize(
    "field", ["summary", "details", "credits", "references", "database_specific"]
)
def test_wrong_metadata_type_fails_closed(field: str) -> None:
    data = vulnerable()
    package(data)["vulnerabilities"][0][field] = 7
    with pytest.raises(DependencyEvidenceError, match="^INVALID_DEPENDENCY_REPORT$"):
        project(data)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"[]",
        b"null",
        b"true",
        b"1",
        b"{} trailing",
        b'{"results": [], "results": []}',
        b'{"results": NaN}',
        b'{"results": Infinity}',
        b"\xff",
        b"{}\nerror",
    ],
)
def test_malformed_truncated_duplicate_or_non_json_never_passes(raw: bytes) -> None:
    with pytest.raises(DependencyEvidenceError, match="^INVALID_DEPENDENCY_REPORT$"):
        project_osv_report(raw, root=ROOT)


def test_oversized_and_excessively_nested_input_fail_safely() -> None:
    for raw in (b" " * (MAX_REPORT_BYTES + 1), b"[" * 2000 + b"]" * 2000):
        with pytest.raises(DependencyEvidenceError, match="^INVALID_DEPENDENCY_REPORT$"):
            project_osv_report(raw, root=ROOT)


@pytest.mark.parametrize(
    "level", ["root", "result", "source", "package", "identity", "vulnerability", "group"]
)
def test_unknown_structural_fields_cannot_hide_vulnerabilities(level: str) -> None:
    data = vulnerable()
    targets = {
        "root": data,
        "result": data["results"][0],
        "source": data["results"][0]["source"],
        "package": package(data),
        "identity": package(data)["package"],
        "vulnerability": package(data)["vulnerabilities"][0],
        "group": package(data)["groups"][0],
    }
    targets[level]["unrecognized_findings"] = [{"id": "CVE-2026-10002"}]
    with pytest.raises(DependencyEvidenceError):
        project(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("vulnerabilities", {}),
        ("vulnerabilities", None),
        ("groups", "hidden"),
        ("groups", None),
        ("license_violations", {}),
        ("dependency_groups", False),
    ],
)
def test_wrong_finding_container_types_fail(field: str, value: Any) -> None:
    data = vulnerable()
    package(data)[field] = value
    with pytest.raises(DependencyEvidenceError):
        project(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_vulnerability",
        "missing_group",
        "orphan_id",
        "duplicate_id",
        "duplicate_vulnerability",
        "empty_group",
        "bad_severity",
        "bad_analysis",
    ],
)
def test_group_membership_and_severity_inconsistencies_fail(mutation: str) -> None:
    data = vulnerable()
    pkg = package(data)
    if mutation == "missing_vulnerability":
        pkg["vulnerabilities"] = []
    elif mutation == "missing_group":
        pkg["groups"] = []
    elif mutation == "orphan_id":
        pkg["groups"][0]["ids"].append("CVE-2026-10002")
    elif mutation == "duplicate_id":
        pkg["groups"].append(copy.deepcopy(pkg["groups"][0]))
    elif mutation == "duplicate_vulnerability":
        pkg["vulnerabilities"].append(copy.deepcopy(pkg["vulnerabilities"][0]))
    elif mutation == "empty_group":
        pkg["groups"][0]["ids"] = []
    elif mutation == "bad_severity":
        pkg["vulnerabilities"][0]["severity"] = [{"type": {}, "score": "9.8"}]
    else:
        pkg["groups"][0]["experimental_analysis"] = {
            "CVE-2026-10002": {"called": True, "unimportant": False}
        }
    with pytest.raises(DependencyEvidenceError):
        project(data)


@pytest.mark.parametrize(
    "channel",
    ["experimental_generic_findings", "experimental_pes", "image_metadata", "license_summary"],
)
def test_unsupported_nonempty_channels_are_not_silently_dropped(channel: str) -> None:
    data = fixture()
    target = data["results"][0] if channel == "experimental_pes" else data
    target[channel] = [{"generated": "finding"}]
    with pytest.raises(DependencyEvidenceError):
        project(data)


@pytest.mark.parametrize(
    "mutation", ["empty", "missing", "duplicate", "foreign", "traversal", "type", "empty_packages"]
)
def test_incomplete_or_wrong_source_coverage_fails(mutation: str) -> None:
    data = fixture()
    if mutation == "empty":
        data["results"] = []
    elif mutation == "missing":
        data["results"].pop()
    elif mutation == "duplicate":
        data["results"].append(copy.deepcopy(data["results"][0]))
    elif mutation == "foreign":
        data["results"][0]["source"]["path"] = "/foreign/uv.lock"
    elif mutation == "traversal":
        data["results"][0]["source"]["path"] = "training/../uv.lock"
    elif mutation == "type":
        data["results"][0]["source"]["type"] = "sbom"
    else:
        data["results"][0]["packages"] = []
    with pytest.raises(DependencyEvidenceError):
        project(data)


def test_withdrawn_uncalled_and_unimportant_are_visible_and_conservatively_fail() -> None:
    data = vulnerable()
    pkg = package(data)
    pkg["vulnerabilities"][0]["withdrawn"] = "2026-01-01T00:00:00Z"
    pkg["groups"][0]["experimental_analysis"] = {
        "GHSA-aaaa-bbbb-cccc": {"called": False, "unimportant": True}
    }
    result = project(data)
    assert result["status"] == "FAIL"
    assert package(result)["vulnerabilities"][0]["withdrawn"] is True
    assert (
        package(result)["groups"][0]["experimental_analysis"]
        == pkg["groups"][0]["experimental_analysis"]
    )
    assert result["counts"]["vulnerability_occurrences"] == 1
    assert result["counts"]["withdrawn_vulnerability_occurrences"] == 1
    assert result["counts"]["uncalled_groups"] == 1
    assert result["counts"]["unimportant_groups"] == 1


def test_commit_identified_package_retains_empty_version_and_commit() -> None:
    data = fixture()
    identity = package(data)["package"]
    identity["version"] = ""
    identity["commit"] = "a" * 40
    result = project(data)
    assert package(result)["package"]["version"] == ""
    assert package(result)["package"]["commit"] == "a" * 40
    del identity["commit"]
    with pytest.raises(DependencyEvidenceError):
        project(data)


def test_missing_severity_type_is_not_assumed_clean() -> None:
    data = vulnerable()
    package(data)["vulnerabilities"][0]["severity"] = [{"score": "1"}]
    with pytest.raises(DependencyEvidenceError):
        project(data)


@pytest.mark.parametrize("kind", ["license", "deprecated"])
def test_other_reported_findings_still_fail(kind: str) -> None:
    data = fixture()
    if kind == "license":
        package(data)["license_violations"] = ["GPL-3.0-only"]
    else:
        package(data)["package"]["deprecated"] = True
    result = project(data)
    assert result["status"] == "FAIL" and result["findings_present"]
    assert (
        result["counts"]["license_violations" if kind == "license" else "deprecated_packages"] == 1
    )


def test_suspicious_essential_identity_is_preserved_for_unchanged_public_screen() -> None:
    data = fixture()
    contact = "generated-name" + "@" + "noncustomer-mail" + ".dev"
    package(data)["package"]["name"] = contact
    result = project(data)
    assert package(result)["package"]["name"] == contact
    assert inspect_bytes("projection.json", json.dumps(result).encode(), load_policy(ROOT))
