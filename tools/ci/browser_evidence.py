"""Retain only a verified browser artifact inventory; raw traces stay unexported."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tools.public_content_check import MAX_FILE_BYTES, digest, inspect_bytes, load_policy


def local_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Unsafe browser evidence path")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Browser evidence escapes repository")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError("Browser evidence link refused")
        current = current.parent
    if not candidate.is_file():
        raise ValueError("Browser evidence is missing")
    return candidate


def retain_browser_evidence(root: Path, command: dict[str, Any]) -> dict[str, Any]:
    steps = command.get("steps", [])
    if len(steps) != 1 or steps[0].get("runner") != "playwright":
        raise ValueError("Unexpected browser command inventory")
    report = steps[0]["tests"]
    nonce = report.get("binding_nonce")
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise ValueError("Browser report nonce is invalid")
    prefix = "evidence/OT-BOOT-C06/runs/" + nonce + "/"
    path = local_file(root, prefix + "evidence.json")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Browser manifest exceeds bound")
    raw = path.read_bytes()
    value = json.loads(raw)
    normalized_report = copy.deepcopy(value.get("report"))
    if not isinstance(normalized_report, dict) or not isinstance(
        normalized_report.get("tests"), list
    ):
        raise ValueError("Browser manifest has no actual test inventory")
    for test in normalized_report["tests"]:
        if not isinstance(test, dict) or not isinstance(test.get("id"), str):
            raise ValueError("Browser test identity is invalid")
        test["id"] = "sha256:" + digest(test["id"].encode())
    if (
        value.get("schema_version") != "opentour.browser-artifacts.v1"
        or value.get("issue_key") != "OPE-127"
        or value.get("kind") != "simulated"
        or value.get("product_acceptance") != "NOT_RUN"
        or value.get("git_worktree_dirty") is not False
        or value.get("commit_sha") != command.get("commit_sha")
        or normalized_report != report
        or value.get("target") != "apps/web/tests/browser-harness.browser.spec.ts"
        or value.get("baseline_review_request") is not None
    ):
        raise ValueError("Browser manifest source/report binding failed")
    source = local_file(root, value["target"])
    if value.get("test_source_sha256") != digest(source.read_bytes()):
        raise ValueError("Browser test source changed")
    outputs = value.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 1000:
        raise ValueError("Browser output inventory is missing or unbounded")
    seen: set[str] = set()
    total = 0
    for output in outputs:
        if not isinstance(output, dict) or set(output) != {"path", "sha256"}:
            raise ValueError("Invalid browser artifact record")
        name = output["path"]
        if (
            not isinstance(name, str)
            or not (name.startswith(prefix + "artifacts/") or name == prefix + "runner.log")
            or name in seen
        ):
            raise ValueError("Browser artifact scope mismatch")
        seen.add(name)
        file = local_file(root, name)
        size = file.stat().st_size
        total += size
        if size > 33_554_432 or total > 134_217_728:
            raise ValueError("Browser artifact inventory exceeds verification bound")
        actual = hashlib.sha256()
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65_536), b""):
                actual.update(chunk)
        if actual.hexdigest() != output["sha256"]:
            raise ValueError("Browser artifact digest mismatch")
    if inspect_bytes("browser-manifest.json", raw, load_policy(root), binary_content=True):
        raise ValueError("Browser manifest disclosure refused")
    return {
        "manifest_sha256": digest(raw),
        "manifest": value,
        "verified_output_files": len(outputs),
        "verified_output_bytes": total,
        "raw_artifacts_uploaded": False,
    }
