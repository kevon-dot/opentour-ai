"""CI publication policy probes; generated outputs are not live vulnerability scans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import pytest

from tools.ci import run as ci_run
from tools.ci.dependency_evidence import DependencyEvidenceError
from tools.public_content_check import digest
from tools.test_runner import ProcessResult

ROOT = Path(__file__).resolve().parents[2]
ScanFixture = tuple[Path, str, str, dict[str, Any], list[tuple[bytes, Path]]]


@pytest.fixture
def scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScanFixture:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/public-content-policy.json").write_bytes(
        (ROOT / ".github/public-content-policy.json").read_bytes()
    )
    # Deliberately unapproved subdomain, but wholly generated reserved-domain data.
    contact = "generated-maintainer" + "@" + "vendor.example.com"
    raw = json.dumps({"results": [], "generated_contact": contact})
    result = ProcessResult(0, stdout=raw)
    projection = {"status": "PASS", "counts": {"packages": 3}, "results": []}
    calls: list[tuple[bytes, Path]] = []

    def project(value: bytes, *, root: Path) -> dict[str, Any]:
        calls.append((value, root))
        return projection

    monkeypatch.setattr(ci_run, "project_osv_report", project)
    monkeypatch.setattr(ci_run, "execute", lambda _: result)
    return tmp_path, raw, contact, projection, calls


def test_only_reviewed_projection_is_published_and_original_digest_is_retained(
    scan: ScanFixture,
) -> None:
    root, raw, contact, projection, calls = scan
    report = ci_run.run_one(ci_run.dependency_scan_command(root), root)
    assert report["status"] == "PASS"
    assert calls == [(raw.encode(), root)]
    assert report["output_sha256"] == digest(raw.encode())
    assert report["command_evidence"] == projection
    assert report["published_projection_sha256"] == digest(
        json.dumps(projection, sort_keys=True).encode()
    )
    assert report["raw_scanner_output_published"] is False
    assert contact not in json.dumps(report)


@pytest.mark.parametrize("failure", ["exit", "timeout", "interrupted", "cleanup"])
def test_clean_projection_cannot_override_process_failure(
    scan: ScanFixture, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, raw, _, _, _ = scan
    result = ProcessResult(
        1 if failure == "exit" else 0,
        stdout=raw,
        timed_out=failure == "timeout",
        interrupted=failure == "interrupted",
        cleanup_complete=failure != "cleanup",
    )
    monkeypatch.setattr(ci_run, "execute", lambda _: result)
    report = ci_run.run_one(ci_run.dependency_scan_command(root), root)
    assert report["status"] == "FAIL"
    assert report["command_evidence"]["status"] == "PASS"


def test_reported_findings_refuse_even_if_scanner_exits_zero(scan: ScanFixture) -> None:
    root, _, _, projection, _ = scan
    projection.update(status="FAIL", findings_present=True)
    report = ci_run.run_one(ci_run.dependency_scan_command(root), root)
    assert report["exit_code"] == 0 and report["status"] == "FAIL"
    assert report["command_evidence"]["findings_present"]


def test_malformed_report_is_not_published_as_empty_success(
    scan: ScanFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, raw, contact, _, _ = scan

    def malformed(*_: Any, **__: Any) -> NoReturn:
        raise DependencyEvidenceError("Generated details must never be published: " + contact)

    monkeypatch.setattr(ci_run, "project_osv_report", malformed)
    report = ci_run.run_one(ci_run.dependency_scan_command(root), root)
    assert report["status"] == "FAIL" and report["reason"] == "INVALID_DEPENDENCY_REPORT"
    assert report["output_sha256"] == digest(raw.encode())
    assert "command_evidence" not in report and contact not in json.dumps(report)


def test_projection_still_uses_unchanged_public_content_screen(scan: ScanFixture) -> None:
    root, _, contact, projection, _ = scan
    projection["package_name"] = contact
    report = ci_run.run_one(ci_run.dependency_scan_command(root), root)
    assert report["status"] == "FAIL" and report["disclosure_findings"]
    assert "command_evidence" not in report and contact not in json.dumps(report)


@pytest.mark.parametrize("changed", ["binary", "argument", "command"])
def test_other_commands_cannot_select_dependency_projection(
    scan: ScanFixture, changed: str
) -> None:
    root, _, contact, _, calls = scan
    command = ci_run.dependency_scan_command(root)
    if changed == "binary":
        command[0] = str(root / "other/osv-scanner")
    elif changed == "argument":
        command.append("--generated-extra")
    else:
        command = ["python", "generated-output.py"]
    report = ci_run.run_one(command, root)
    assert not calls
    assert report["status"] == "FAIL" and report["disclosure_findings"]
    assert "command_evidence" not in report and contact not in json.dumps(report)


@pytest.mark.parametrize("has_advisory", [False, True])
def test_real_projection_composes_with_publication_without_losing_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_advisory: bool
) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/public-content-policy.json").write_bytes(
        (ROOT / ".github/public-content-policy.json").read_bytes()
    )
    data: dict[str, Any] = {
        "results": [
            {
                "source": {"type": "lockfile", "path": str(tmp_path / lock)},
                "packages": [
                    {"package": {"name": "generated-package", "version": "1", "ecosystem": "PyPI"}}
                ],
            }
            for lock in ("uv.lock", "training/uv.lock", "pnpm-lock.yaml")
        ],
        "experimental_config": {"licenses": {"summary": False, "allowlist": None}},
    }
    contact = "generated-maintainer" + "@" + "vendor.example.com"
    if has_advisory:
        data["results"][0]["packages"][0].update(
            vulnerabilities=[
                {
                    "id": "GHSA-aaaa-bbbb-cccc",
                    "credits": [{"name": "Generated maintainer", "contact": [contact]}],
                }
            ],
            groups=[{"ids": ["GHSA-aaaa-bbbb-cccc"], "max_severity": "7.5"}],
        )
    raw = json.dumps(data)
    monkeypatch.setattr(ci_run, "execute", lambda _: ProcessResult(0, stdout=raw))
    report = ci_run.run_one(ci_run.dependency_scan_command(tmp_path), tmp_path)
    assert report["status"] == ("FAIL" if has_advisory else "PASS")
    assert report["exit_code"] == 0 and not report["disclosure_findings"]
    assert contact not in json.dumps(report)
    evidence = report["command_evidence"]
    assert evidence["counts"]["packages"] == evidence["counts"]["sources"] == 3
    assert evidence["counts"]["vulnerability_occurrences"] == int(has_advisory)
    assert evidence["original_output_sha256"] == report["output_sha256"] == digest(raw.encode())
    if has_advisory:
        assert evidence["results"][0]["packages"][0]["vulnerabilities"][0]["id"] == (
            "GHSA-aaaa-bbbb-cccc"
        )
