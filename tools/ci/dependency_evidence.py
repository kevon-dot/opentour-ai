"""Project the pinned OSV source-scan report without publishing advisory prose.

Contract: google/osv-scanner v2.7.1 pkg/models/results.go and the OSV schema
at ossf/osv-schema commit 6410c13304f9. This is a finite three-lockfile adapter,
not a general OSV renderer. Unknown structural fields fail closed. Advisory
text/credits/URLs/database metadata are deliberately not publication fields.
The caller must retain the process outcome and screen these projected bytes
with the unchanged public-content policy before publication.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, NoReturn

MAX_REPORT_BYTES = 1_048_576
LOCKFILES = frozenset({"uv.lock", "training/uv.lock", "pnpm-lock.yaml"})
SCANNER_VERSION = "2.7.1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}\Z")


class DependencyEvidenceError(ValueError):
    """Safe error: never includes scanner payloads, fields or paths."""


def _invalid() -> NoReturn:
    raise DependencyEvidenceError("INVALID_DEPENDENCY_REPORT")


def _object(value: Any, allowed: set[str], required: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() - allowed or (required or set()) - value.keys():
        _invalid()
    return value


def _array(value: Any, *, nullable: bool = False) -> list[Any]:
    if nullable and value is None:
        return []
    if not isinstance(value, list) or len(value) > 10_000:
        _invalid()
    return value


def _text(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 1024 or (not empty and not value):
        _invalid()
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        _invalid()
    return value


def _ids(value: Any, *, nullable: bool = False) -> list[str]:
    values = [_text(item) for item in _array(value, nullable=nullable)]
    if any(not _ID.fullmatch(item) for item in values) or len(set(values)) != len(values):
        _invalid()
    return values


def _strings(value: Any, *, nullable: bool = False) -> list[str]:
    return [_text(item) for item in _array(value, nullable=nullable)]


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        _invalid()
    return value


def _metadata(value: dict[str, Any], types: dict[str, type]) -> None:
    # These fields have no authority to replace the canonical vulnerability list.
    # Type-check but never copy their arbitrary free text into a public report.
    for field, expected in types.items():
        if field in value and not isinstance(value[field], expected):
            _invalid()


def _severity(value: Any) -> list[dict[str, str]]:
    output = []
    for item in _array(value, nullable=True):
        obj = _object(item, {"type", "score", "source"}, {"type", "score"})
        if obj["type"] not in ("CVSS_V2", "CVSS_V3", "CVSS_V4", "Ubuntu"):
            _invalid()
        score = _text(obj["score"])
        if not re.fullmatch(r"[A-Za-z0-9:/.+_-]+", score):
            _invalid()
        result = {"type": obj["type"], "score": score}
        if "source" in obj:
            if obj["source"] not in ("NVD", "CNA", "SELF"):
                _invalid()
            result["source"] = obj["source"]
        output.append(result)
    return output


def _affected(value: Any) -> list[dict[str, Any]]:
    output = []
    for item in _array(value, nullable=True):
        obj = _object(
            item,
            {
                "package",
                "severity",
                "ranges",
                "versions",
                "ecosystem_specific",
                "database_specific",
            },
        )
        _metadata(obj, {"ecosystem_specific": dict, "database_specific": dict})
        result: dict[str, Any] = {"severity": _severity(obj.get("severity", []))}
        if "package" in obj:
            package = _object(obj["package"], {"name", "ecosystem", "purl"}, {"name", "ecosystem"})
            _metadata(package, {"purl": str})
            result["package"] = {key: _text(package[key]) for key in ("name", "ecosystem")}
        result["versions"] = _strings(obj.get("versions", []), nullable=True)
        ranges = []
        for raw_range in _array(obj.get("ranges", []), nullable=True):
            span = _object(
                raw_range, {"type", "repo", "events", "database_specific"}, {"type", "events"}
            )
            if span["type"] not in ("GIT", "SEMVER", "ECOSYSTEM"):
                _invalid()
            _metadata(span, {"repo": str, "database_specific": dict})
            events = []
            for raw_event in _array(span["events"]):
                event = _object(raw_event, {"introduced", "fixed", "last_affected", "limit"})
                if len(event) != 1:
                    _invalid()
                events.append({key: _text(version) for key, version in event.items()})
            if not events:
                _invalid()
            ranges.append({"type": span["type"], "events": events})
        result["ranges"] = ranges
        output.append(result)
    return output


def _vulnerability(value: Any) -> dict[str, Any]:
    obj = _object(
        value,
        {
            "schema_version",
            "id",
            "modified",
            "published",
            "withdrawn",
            "aliases",
            "related",
            "upstream",
            "summary",
            "details",
            "severity",
            "affected",
            "references",
            "credits",
            "database_specific",
        },
        {"id"},
    )
    _metadata(
        obj,
        {
            "schema_version": str,
            "modified": str,
            "published": str,
            "withdrawn": str,
            "summary": str,
            "details": str,
            "database_specific": dict,
        },
    )
    for ref in _array(obj.get("references", []), nullable=True):
        _metadata(_object(ref, {"type", "url"}, {"type", "url"}), {"type": str, "url": str})
    for credit in _array(obj.get("credits", []), nullable=True):
        row = _object(credit, {"name", "contact", "type"}, {"name"})
        _metadata(row, {"name": str, "type": str})
        if any(not isinstance(item, str) for item in _array(row.get("contact", []), nullable=True)):
            _invalid()
    result = {
        "id": _ids([obj["id"]])[0],
        "aliases": _ids(obj.get("aliases", []), nullable=True),
        "related": _ids(obj.get("related", []), nullable=True),
        "upstream": _ids(obj.get("upstream", []), nullable=True),
        "severity": _severity(obj.get("severity", [])),
        "affected": _affected(obj.get("affected", [])),
        "withdrawn": "withdrawn" in obj,
    }
    # The standard severity arrays and scanner group maximum remain intact;
    # database-specific free text is not treated as an alternative finding list.
    return result


def _package(value: Any) -> dict[str, Any]:
    obj = _object(
        value,
        {
            "package",
            "dependency_groups",
            "vulnerabilities",
            "groups",
            "licenses",
            "license_violations",
        },
        {"package"},
    )
    package = _object(
        obj["package"],
        {
            "name",
            "version",
            "ecosystem",
            "purl",
            "commit",
            "os_package_name",
            "deprecated",
            "image_origin_details",
        },
        {"name", "version", "ecosystem"},
    )
    _metadata(package, {"purl": str, "commit": str, "os_package_name": str})
    if package.get("image_origin_details") is not None:
        _invalid()  # This finite adapter does not admit container/image scan evidence.
    identity = {key: _text(package[key]) for key in ("name", "ecosystem")}
    identity["version"] = _text(package["version"], empty=True)
    if not identity["version"] and not package.get("commit"):
        _invalid()
    if "commit" in package:
        identity["commit"] = _text(package["commit"])
    vulnerabilities = [_vulnerability(item) for item in _array(obj.get("vulnerabilities", []))]
    vuln_ids = [item["id"] for item in vulnerabilities]
    if len(set(vuln_ids)) != len(vuln_ids):
        _invalid()
    groups = []
    grouped_ids: list[str] = []
    for item in _array(obj.get("groups", [])):
        group = _object(
            item,
            {"ids", "aliases", "experimental_analysis", "max_severity"},
            {"ids", "max_severity"},
        )
        ids = _ids(group["ids"])
        if not ids:
            _invalid()
        grouped_ids.extend(ids)
        maximum = _text(group["max_severity"], empty=True)
        if maximum and not re.fullmatch(r"(?:10(?:\.0)?|[0-9](?:\.[0-9]+)?|UNKNOWN)", maximum):
            _invalid()
        analysis = group.get("experimental_analysis", {})
        if not isinstance(analysis, dict) or analysis.keys() - set(ids):
            _invalid()
        safe_analysis = {}
        for key, detail in analysis.items():
            detail = _object(detail, {"called", "unimportant"}, {"called", "unimportant"})
            safe_analysis[key] = {name: _bool(flag) for name, flag in detail.items()}
        groups.append(
            {
                "ids": ids,
                "aliases": _ids(group.get("aliases", []), nullable=True),
                "max_severity": maximum,
                "experimental_analysis": safe_analysis,
            }
        )
    # Neither orphan groups nor omitted/duplicated membership can conceal a finding.
    if sorted(grouped_ids) != sorted(vuln_ids):
        _invalid()
    return {
        "package": identity,
        "deprecated": _bool(package.get("deprecated", False)),
        "dependency_groups": _strings(obj.get("dependency_groups", [])),
        "vulnerabilities": vulnerabilities,
        "groups": groups,
        "licenses": _strings(obj.get("licenses", [])),
        "license_violations": _strings(obj.get("license_violations", [])),
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def project_osv_report(raw: bytes, *, root: Path) -> dict[str, Any]:
    """Return bounded publication fields; no status overrides or external I/O.

    An empty scan, missing lockfile, malformed JSON, unknown structural field,
    unsupported finding channel or inconsistent group is an error, never clean.
    The caller must independently enforce native exit code, timeout and cleanup.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REPORT_BYTES:
        _invalid()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: _invalid(),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise DependencyEvidenceError("INVALID_DEPENDENCY_REPORT") from None
    obj = _object(
        value,
        {
            "results",
            "experimental_config",
            "experimental_generic_findings",
            "image_metadata",
            "license_summary",
        },
        {"results", "experimental_config"},
    )
    if (
        _array(obj.get("experimental_generic_findings", []))
        or obj.get("image_metadata") is not None
    ):
        _invalid()  # Never discard an unsupported finding channel or image scan.
    config = _object(obj["experimental_config"], {"licenses"}, {"licenses"})
    licenses = _object(config["licenses"], {"summary", "allowlist"}, {"summary", "allowlist"})
    safe_config = {
        "summary": _bool(licenses["summary"]),
        "allowlist": _strings(licenses["allowlist"], nullable=True),
    }
    if safe_config["summary"] or safe_config["allowlist"] or obj.get("license_summary", []) != []:
        _invalid()  # This command has no license-scan flags; retain violations below regardless.
    results = []
    seen: set[str] = set()
    counts = {
        "sources": 0,
        "packages": 0,
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
    all_ids: set[str] = set()
    for item in _array(obj["results"]):
        result = _object(item, {"source", "packages", "experimental_pes"}, {"source", "packages"})
        if _array(result.get("experimental_pes", [])):
            _invalid()
        source = _object(result["source"], {"type", "path"}, {"type", "path"})
        if source["type"] != "lockfile":
            _invalid()
        path = _text(source["path"])
        # Exact string matching avoids accepting traversal, aliases or foreign paths.
        names = [name for name in LOCKFILES if path in (name, str(root / name))]
        if len(names) != 1 or names[0] in seen:
            _invalid()
        seen.add(names[0])
        packages = [_package(package) for package in _array(result["packages"])]
        if not packages:
            _invalid()
        for package in packages:
            counts["packages"] += 1
            counts["affected_packages"] += bool(package["vulnerabilities"])
            counts["vulnerability_occurrences"] += len(package["vulnerabilities"])
            counts["groups"] += len(package["groups"])
            counts["license_violations"] += len(package["license_violations"])
            counts["deprecated_packages"] += package["deprecated"]
            counts["withdrawn_vulnerability_occurrences"] += sum(
                item["withdrawn"] for item in package["vulnerabilities"]
            )
            for group in package["groups"]:
                analysis = group["experimental_analysis"]
                counts["uncalled_groups"] += bool(analysis) and not any(
                    item["called"] for item in analysis.values()
                )
                counts["unimportant_groups"] += any(
                    item["unimportant"] for item in analysis.values()
                )
            all_ids.update(item["id"] for item in package["vulnerabilities"])
        results.append({"source": {"type": "lockfile", "path": names[0]}, "packages": packages})
    if seen != LOCKFILES:
        _invalid()
    counts["sources"] = len(results)
    counts["unique_vulnerabilities"] = len(all_ids)
    findings_present = any(
        counts[key]
        for key in ("vulnerability_occurrences", "license_violations", "deprecated_packages")
    )
    return {
        "schema_version": "opentour.dependency-evidence.v1",
        "scanner_version": SCANNER_VERSION,
        "original_output_sha256": hashlib.sha256(raw).hexdigest(),
        "original_output_bytes": len(raw),
        "counts": counts,
        "findings_present": findings_present,
        "status": "FAIL" if findings_present else "PASS",
        "finding_policy": "all_reported_findings_block",
        "results": results,
    }
